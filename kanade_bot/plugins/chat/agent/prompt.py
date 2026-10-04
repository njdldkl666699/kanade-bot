"""聊天Agent的系统提示词组装。

提示词不再是一整块写死的字符串，而是「若干片段文件 + 变量替换」，并按
**变化频率**分成两层：

- `ChatPromptConfig.files`（静态层）：只放进程恒定的内容，构造时求值一次，
  渲染为多个 `InstructionPart(dynamic=False)`；
- `ChatPromptConfig.sections`（会话层）：只放每会话恒定的内容，每轮求值，
  渲染为单个 `dynamic=True` 的 part。

分层的理由不是渲染开销（19KB 正则替换一轮 ~100µs，相对一次模型调用可以忽略），
而是 **provider 前缀缓存**：系统提示词位于消息列表最前面，任何一个字节变化，
后面整块都缓存失效。时间尤其致命——它每分钟变一次，等于每轮必崩缓存，
所以当前时间改由用户消息携带（见 `current_time_line`），不进系统提示词。

## 模板语法

沿用项目既有的 `{{变量名}}` 双花括号写法，按**已知变量名**做替换，
未知占位符原样保留。

「只替换已知变量」不是偷懒，而是必需的：提示词里 `{{happy}}`、`{{表情包名称}}`
这类表情包引用是运行时约定（`chat.py` 从回复里解析出来、换成表情包图片），
与模板语法同形。若改成单花括号来避开冲突，代价是彻底失去拼写校验——
写错的名字会原样发给模型，只能靠肉眼发现；而双花括号至少能用正则精确识别，
把「像变量名的未知占位符」单独记一条日志。

片段文件在 `PromptRenderer` 构造时一次性读入缓存（提示词不随运行变化），
每次 `render()` 只做变量替换，因此没有运行期文件 IO。
"""

import platform as _platform
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path

from nonebot import get_driver, logger
from pydantic_ai.messages import InstructionPart

from kanade_bot.utils.parse import build_sender_info

from ..config import ScopedConfig, cfg, chat_configs
from .deps import ChatDeps
from .prompt_config import ChatPromptConfig

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
"""模板占位符：仅匹配形如 `{{ 变量名 }}` 的合法标识符。

表情包占位符 `{{happy}}` 同样会命中，但 `happy` 不是已知变量名，
因此会原样保留；`{{表情包名称}}` 因含中文则根本不匹配。"""

FALSE_VALUES = frozenset({"", "false", "0", "no", "off", "none", "null"})
"""`when` 判定为假时的取值（大小写不敏感）"""

WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
"""`weekday` 的取值表，下标为 `datetime.weekday()`"""


def current_time_line() -> str:
    """当前时间的单行表述，附在**用户消息**里而不是系统提示词里

    系统提示词位于消息前缀，任何字节变化都会让整块缓存失效；时间每分钟变一次，
    放进去等于每轮重新计费。改由用户消息携带后，系统提示词保持字节稳定。
    """
    now = datetime.now().astimezone()
    return f"$ 现在是 {now:%Y-%m-%d %H:%M:%S}，{WEEKDAY_NAMES[now.weekday()]}"


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _bot_name() -> str:
    """Bot 昵称（NoneBot 配置可能是字符串或列表）"""
    nickname = get_driver().config.nickname
    if isinstance(nickname, str):
        return nickname
    return "、".join(nickname)


def is_truthy(value: str | None) -> bool:
    """判定 `when` 条件的真假：非空且不在 `FALSE_VALUES` 中即为真"""
    if value is None:
        return False
    return value.strip().lower() not in FALSE_VALUES


@cache
def _env_info() -> tuple[str, str, str, str]:
    """`(os, os_release, machine, python_version)`，进程内恒定故只取一次"""
    return (
        _platform.system(),
        _platform.release(),
        _platform.machine(),
        _platform.python_version(),
    )


def _builtin_vars() -> dict[str, str]:
    """内置变量：环境信息（进程内恒定）"""

    os_, os_release, machine, python_version = _env_info()
    return {
        "os": os_,
        "os_release": os_release,
        "machine": machine,
        "python_version": python_version,
    }


@dataclass(frozen=True)
class PromptFragment:
    """已读入内存的提示词片段"""

    name: str
    """片段来源文件名，仅用于日志与 `InstructionPart.name`"""

    content: str
    """片段内容（未替换变量的模板）"""

    when: str | None = None
    """条件变量名，`None` 或空串表示无条件生效"""

    @property
    def stem(self) -> str:
        """去掉扩展名的文件名，作为 `InstructionPart.name`"""
        return self.name.rsplit(".", 1)[0]


class PromptRenderer:
    """模块化提示词渲染器

    构造时按配置读入全部片段并缓存，`render()` 只做「条件过滤 → 变量替换 →
    拼接」。变量优先级（后者覆盖前者）：内置变量 → `base_vars` →
    `ChatPromptConfig.vars` → `render()` 传入的运行时变量。
    """

    def __init__(
        self,
        config: ChatPromptConfig,
        *,
        base_vars: Mapping[str, str] | None = None,
        root: Path | None = None,
    ):
        self._config = config
        self._base_vars = dict(base_vars or {})
        self._files: list[PromptFragment] = []
        self._sections: list[PromptFragment] = []
        self._load(root)
        logger.debug(
            f"已加载静态层{len(self._files)}个片段{[f.name for f in self._files]}，"
            f"会话层{len(self._sections)}个片段{[f.name for f in self._sections]}"
        )

    @property
    def files(self) -> list[PromptFragment]:
        """已加载的静态层片段"""
        return list(self._files)

    @property
    def sections(self) -> list[PromptFragment]:
        """已加载的会话层片段"""
        return list(self._sections)

    def _load(self, root: Path | None) -> None:
        """读入全部片段文件，缺失的记警告后跳过"""
        base = root or self._config.dir_path
        self._files = self._read(base, [(f, None) for f in self._config.files])
        self._sections = self._read(base, [(s.file, s.when or None) for s in self._config.sections])

    @staticmethod
    def _read(base: Path, entries: list[tuple[str, str | None]]) -> list[PromptFragment]:
        fragments: list[PromptFragment] = []
        for name, when in entries:
            path = base / name
            if not path.is_file():
                logger.warning(f"提示词文件不存在，已跳过：{path.absolute()}")
                continue
            fragments.append(PromptFragment(name, path.read_text(encoding="utf-8").strip(), when))
        return fragments

    def render_files(self, variables: Mapping[str, str] | None = None) -> list[InstructionPart]:
        """渲染静态层：每个片段一个具名 `dynamic=False` part

        片段之间由框架用空行连接（`InstructionPart.join`），无需手动加分隔线。
        """
        values = self._values(variables)
        return [
            # 用去掉扩展名的文件名做 name：id 会变成 `agent:identity` 这样可外部
            # 寻址的短键，比 `agent:identity.md` 好用（`.` 在 id 里合法，但没必要）
            InstructionPart(content=self._substitute(f, values), name=f.stem)
            for f in self._files
        ]

    def render_sections(self, variables: Mapping[str, str] | None = None) -> str:
        """渲染会话层：`when` 不满足的片段跳过，其余用空行拼成单个字符串

        只能是单个字符串——Pydantic AI 的 instructions 函数签名是 `str | None`，
        返回不了 part 列表。好在这层体积小，整体落在缓存边界之后的动态段。
        """
        values = self._values(variables)
        parts: list[str] = []
        for fragment in self._sections:
            when = fragment.when
            if when and not is_truthy(values.get(when)):
                logger.debug(f"提示词片段{fragment.name}因条件{when}不满足而跳过")
                continue
            parts.append(self._substitute(fragment, values))
        return "\n\n".join(p for p in parts if p)

    def _values(self, variables: Mapping[str, str] | None) -> dict[str, str]:
        """合并变量，后者覆盖前者：
        内置环境变量 → `base_vars` → `ChatPromptConfig.vars` → 调用方传入
        """
        values: dict[str, str] = _builtin_vars()
        values.update(self._base_vars)
        values.update(self._config.vars)
        values.update(variables or {})
        return values

    @staticmethod
    def _substitute(fragment: PromptFragment, values: Mapping[str, str]) -> str:
        """替换片段中的已知变量，未知占位符（如表情包 `{{happy}}`）原样保留"""
        unresolved: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in values:
                return values[key]
            unresolved.add(key)
            return match.group(0)

        content = PLACEHOLDER_PATTERN.sub(replace, fragment.content)
        if unresolved:
            logger.debug(f"提示词片段{fragment.name}存在未知占位符：{sorted(unresolved)}")
        return content


class ChatPrompt:
    """聊天Agent的提示词渲染器

    静态层在构造时一次性渲染成 `InstructionPart` 列表（`dynamic=False`），
    会话层每轮渲染成一个字符串（`dynamic=True`）。时间不进任何一层，
    由 `current_time_line()` 附到用户消息上。
    """

    def __init__(self, scoped_cfg: ScopedConfig = cfg):
        self._agent_cfg = scoped_cfg.agent
        self._mcp_available = False
        self._renderer = PromptRenderer(
            self._agent_cfg.prompt,
            base_vars={
                "bot_name": _bot_name(),
                "model": self._agent_cfg.model or "",
            },
        )
        self._static_parts = self._renderer.render_files()

    def set_mcp_available(self, available: bool) -> None:
        """MCP 连接完成后回填 `mcp_enabled`

        提示词关心的是「模型手上真的有联网工具」，而不是「配置里写了几个服务器」，
        故不能在构造时用配置推断。
        """
        self._mcp_available = available

    @property
    def static_instructions(self) -> list[InstructionPart]:
        """静态层片段（`dynamic=False`），进程内不变，可被 provider 前缀缓存命中"""
        return list(self._static_parts)

    @property
    def fragment_names(self) -> list[str]:
        """已加载的片段文件名（调试用）"""
        return [f.name for f in (*self._renderer.files, *self._renderer.sections)]

    def session_instructions(self, deps: ChatDeps) -> str:
        """按当前会话渲染会话层片段"""
        return self._renderer.render_sections(self._session_vars(deps))

    def _session_vars(self, deps: ChatDeps) -> dict[str, str]:
        """会话层变量，每会话恒定（发送者除外：群聊里逐轮切换，
        但已由 `_build_send_prompt` 写进用户消息，故不进系统提示词）"""
        info = deps.session_info
        memory_context = deps.memory_context

        return {
            "platform": info.platform or "",
            "session_id": info.session_id,
            "user_info": build_sender_info(info.nickname, info.user_id),
            "group_info": build_sender_info(info.group_name, info.group_id),
            "is_group": _bool_str(bool(info.group_id)),
            # 沙箱
            "sandbox_enabled": _bool_str(deps.sandbox is not None),
            "workspace_root": deps.sandbox_root or "",
            # 功能开关：与 `prepare_tools` 的过滤口径保持一致
            "mcp_enabled": _bool_str(self._mcp_available),
            "vision_enabled": _bool_str(self._agent_cfg.vision),
            "memories_enabled": _bool_str(bool(memory_context and memory_context.scopes())),
            "tts_enabled": _bool_str(bool(cfg.tts.base_url)),
            "memes_enabled": _bool_str(bool(chat_configs.instance.memes)),
            "rag_enabled": _bool_str(cfg.rag.enabled),
        }

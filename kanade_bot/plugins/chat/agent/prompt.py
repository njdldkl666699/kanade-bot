import platform
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path

from nonebot import get_driver, logger
from pydantic_ai.messages import InstructionPart

from kanade_bot.utils.parse import build_sender_info

from ..config import ChatPromptConfig, ScopedConfig, cfg, chat_configs
from .deps import ChatDeps

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
"""模板占位符

仅匹配形如 `{{ 变量名 }}` 的合法标识符，未知变量名会原样保留。
"""

FALSE_VALUES = frozenset({"", "false", "0", "no", "off", "none", "null"})
"""`when` 判定为假时的取值"""

WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
"""`weekday` 的取值表"""


def current_time_line() -> str:
    """当前时间的单行表述"""
    now = datetime.now().astimezone()
    return f"$ 现在是 {now:%Y-%m-%d %H:%M:%S}，{WEEKDAY_NAMES[now.weekday()]}"


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _bot_name() -> str:
    """Bot 昵称"""
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
def _builtin_vars() -> dict[str, str]:
    """内置变量：环境信息"""
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
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
    """模块化提示词渲染器"""

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
        """渲染静态层"""
        values = self._values(variables)
        return [
            InstructionPart(content=self._substitute(f, values), name=f.stem) for f in self._files
        ]

    def render_sections(self, variables: Mapping[str, str] | None = None) -> str:
        """渲染会话层：`when` 不满足的片段跳过，其余用空行拼成单个字符串"""
        values = self._values(variables)
        parts: list[str] = []
        for fragment in self._sections:
            when = fragment.when
            if when and not is_truthy(values.get(when)):
                logger.trace(f"提示词片段{fragment.name}因条件{when}不满足而跳过")
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
        """替换片段中的已知变量"""
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
    """聊天Agent的提示词渲染器"""

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
        """MCP 连接完成后回填 `mcp_enabled`"""
        self._mcp_available = available

    @property
    def static_instructions(self) -> list[InstructionPart]:
        """静态层片段"""
        return self._static_parts

    def session_instructions(self, deps: ChatDeps) -> str:
        """按当前会话渲染会话层片段"""
        return self._renderer.render_sections(self._session_vars(deps))

    def _session_vars(self, deps: ChatDeps) -> dict[str, str]:
        """会话层变量，每会话恒定"""
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

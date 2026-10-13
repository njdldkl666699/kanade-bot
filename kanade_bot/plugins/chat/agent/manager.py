"""聊天会话管理器：共享运行时持有者与每会话对象注册表"""

import json
from collections import deque

from nonebot import get_driver, logger
from pydantic_ai import AbstractToolset, Agent, FilteredToolset, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai_backends import PERMISSIVE_RULESET, ConsoleCapability

from kanade_bot.utils.pai_runtime import (
    build_model_settings,
    get_model,
    resolve_model_context_window,
)

from ..config import cfg
from .compaction import RequestBudgetCapability, build_compaction_capability
from .deps import ChatDeps
from .memory import MemoryStore
from .prompt import ChatPrompt
from .sandbox import SandboxManager, SandboxWorkspaceCapability
from .session import ChatSession
from .session_store import SessionStore
from .tool import build_tools


class ChatSessionManager:
    """聊天会话管理器：共享运行时（Agent/存储/MCP/沙箱池）+ 会话对象注册表"""

    def __init__(self):
        self.store = SessionStore(cfg.session.sessions_dir_path)
        """会话存储"""

        self.memory_store = MemoryStore(
            cfg.memory.database_file_path,
            max_memories_per_scope=cfg.memory.max_records_per_scope,
        )
        """记忆存储"""

        self.compaction_params = cfg.compaction
        # 窗口未显式配置时解析并固化；固化值进入 mark 指纹，跨进程漂移可检测
        if self.compaction_params.context_window is None and (
            window := resolve_model_context_window(cfg.agent)
        ):
            self.compaction_params = self.compaction_params.model_copy(
                update={"context_window": window}
            )
            logger.info(f"压缩阈值窗口已固化为主模型解析值：{window} tokens")
        self.model_settings = build_model_settings(cfg.agent)
        self.output_upper_tokens = self.model_settings.get("max_tokens")
        self.compaction = build_compaction_capability(
            self.compaction_params,
            output_upper_tokens=self.output_upper_tokens,
            model_settings=self.model_settings,
        )
        self.request_budget = RequestBudgetCapability(
            context_window=self.compaction_params.context_window,
            output_upper_tokens=self.output_upper_tokens,
        )

        self._prompt = ChatPrompt(cfg)
        """模块化系统提示词渲染器"""

        self._sessions: dict[str, ChatSession] = {}
        """每会话运行时对象注册表"""

        self.mcp_toolsets: list[AbstractToolset[ChatDeps]] = []
        """MCP 工具集"""
        self.sandbox_manager: SandboxManager | None = None

        capabilities: list[AbstractCapability] = [self.compaction, self.request_budget]
        if cfg.sandbox.enabled:
            self.sandbox_manager = SandboxManager()
            capabilities.extend(
                [
                    SandboxWorkspaceCapability(),
                    ConsoleCapability(
                        permissions=PERMISSIVE_RULESET,
                        image_support=cfg.agent.vision,
                        profile="agent",
                    ),
                ]
            )

        self.agent: Agent[ChatDeps] = Agent(
            name="kanade-bot-chat",
            instructions=[
                *self._prompt.static_instructions,
                self._session_instructions,
            ],
            deps_type=ChatDeps,
            model=get_model(cfg.agent),
            model_settings=self.model_settings,
            tools=build_tools(),
            capabilities=capabilities,
        )

        driver = get_driver()
        driver.on_startup(self._start_mcp)
        driver.on_startup(self._load_sessions_messages_cache)
        driver.on_shutdown(self._save_sessions_messages_cache)
        driver.on_shutdown(self._shutdown)

    # ===== 会话注册表 =====

    def get_session(self, session_id: str) -> ChatSession:
        """获取会话对象，不存在时创建"""
        session = self._sessions.get(session_id)
        if session is None:
            session = ChatSession(session_id, manager=self)
            self._sessions[session_id] = session
        return session

    # ===== 消息缓冲区缓存 =====

    def _load_sessions_messages_cache(self):
        """加载会话消息缓冲区缓存"""
        cache_file = cfg.session.buffer_cache_file_path
        if not cache_file.is_file():
            logger.info(f"会话消息缓冲区缓存文件不存在，路径: {cache_file.absolute()}")
            return

        try:
            with cache_file.open("r", encoding="utf-8") as f:
                data: dict[str, list[str]] = json.load(f)
        except Exception as e:
            logger.exception(f"加载会话消息缓冲区缓存时发生错误: {e}")
            return

        for session_id, messages in data.items():
            self.get_session(session_id).buffer = deque(
                messages, maxlen=cfg.session.buffer_max_size
            )
        logger.info(f"已加载{len(data)}个会话的消息缓冲区缓存")

    def _save_sessions_messages_cache(self):
        """保存会话消息缓冲区缓存"""
        cache_file = cfg.session.buffer_cache_file_path
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        data = {session_id: list(session.buffer) for session_id, session in self._sessions.items()}
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.info(f"已保存{len(data)}个会话的消息缓冲区缓存")
        except Exception as e:
            logger.exception(f"保存会话消息缓冲区缓存时发生错误: {e}")

    # ===== 生命周期 =====

    async def _start_mcp(self) -> None:
        """连接MCP服务器并把可用工具集挂到Agent"""
        mcp_configs = cfg.agent.mcp_servers
        if not mcp_configs:
            return

        toolsets: list[AbstractToolset[ChatDeps]] = []
        failed: list[str] = []
        for name, server_cfg in mcp_configs.items():
            headers = dict(server_cfg.headers) if server_cfg.headers else None
            try:
                toolset: AbstractToolset[ChatDeps] = MCPToolset(server_cfg.url, headers=headers)
                await toolset.__aenter__()
            except Exception as e:
                logger.warning(f"MCP服务器{name}连接失败: {e}")
                failed.append(name)
                continue

            allowed = server_cfg.tools
            if allowed and allowed != ["*"]:
                # 工具白名单：关闭的工具直接不进 schema
                allow_set = frozenset(allowed)
                toolset = FilteredToolset(
                    toolset, lambda ctx, td, allow=allow_set: td.name in allow
                )

            toolsets.append(toolset)

        if failed:
            logger.warning(f"MCP服务器连接失败: {failed}")
        self.mcp_toolsets = toolsets
        self._prompt.set_mcp_available(bool(toolsets))
        logger.info(f"已加载{len(toolsets)}个MCP工具集")

    async def _shutdown(self):
        if self.sandbox_manager is not None:
            await self.sandbox_manager.close_all()
        for toolset in self.mcp_toolsets:
            try:
                await toolset.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"关闭MCP服务器时发生错误: {e}")
        self.mcp_toolsets = []

    # ===== Agent 会话层指令 =====

    def _session_instructions(self, ctx: RunContext[ChatDeps]) -> str:
        """按当前会话渲染会话层提示词"""
        return self._prompt.session_instructions(ctx.deps)


chat_manager = ChatSessionManager()

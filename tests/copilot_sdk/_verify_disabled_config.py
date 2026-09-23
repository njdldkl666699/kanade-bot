"""验证 disabled_mcp_servers 字段的配置解析与 session_config 透传

不需要启动 nonebot/运行时，仅验证 Pydantic 校验和 model_dump_session_config。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from kanade_bot.utils.schema import BaseAgentConfig  # noqa: E402


class _ChatConfig(BaseAgentConfig):
    """测试用：模拟 chat 插件的 ScopedConfig（继承 BaseAgentConfig）"""


def main() -> None:
    data = yaml.safe_load(Path("config-prod.yaml").read_text(encoding="utf-8"))
    raw = data["chat"]
    raw.setdefault("system_prompt_file", "Chat.md")

    cfg = _ChatConfig.model_validate(raw)
    session_config = cfg.model_dump_session_config()

    print(f"disabled_mcp_servers = {cfg.disabled_mcp_servers!r}")
    print(f"session_config 透传 = {session_config.get('disabled_mcp_servers')!r}")
    assert cfg.disabled_mcp_servers == ["github-mcp-server"]
    assert session_config["disabled_mcp_servers"] == ["github-mcp-server"]

    # 未设置时 None 透传（exclude_unset 语义下不出现）
    empty = _ChatConfig(system_prompt_file="x")
    empty_config = empty.model_dump_session_config()
    assert "disabled_mcp_servers" not in empty_config, "未设置时不应出现在 session_config"
    print("未设置时不透传: OK")

    # resume/create_session 均接受 **session_config（SDK 签名已含该参数）
    import inspect

    from copilot import CopilotClient

    for method in ("create_session", "resume_session"):
        sig = inspect.signature(getattr(CopilotClient, method))
        assert "disabled_mcp_servers" in sig.parameters, method
        print(f"CopilotClient.{method} 接受 disabled_mcp_servers: OK")

    print("\n全部验证通过")


if __name__ == "__main__":
    main()

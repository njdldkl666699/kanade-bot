"""文件权限处理器单元测试

以文件路径直接加载permissions.py，避免触发nonebot插件导入链。

运行: .venv/bin/python tests/fs_permission_test.py
"""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from copilot.session_events import (
    PermissionRequestCustomTool,
    PermissionRequestMcp,
    PermissionRequestMemory,
    PermissionRequestRead,
    PermissionRequestShell,
    PermissionRequestUrl,
    PermissionRequestWrite,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "permissions", REPO_ROOT / "kanade_bot/plugins/chat/agent/permissions.py"
)
permissions = importlib.util.module_from_spec(spec)
sys.modules["permissions"] = permissions
spec.loader.exec_module(permissions)


def write(file_name: str, **kw) -> PermissionRequestWrite:
    return PermissionRequestWrite(
        can_offer_session_approval=True,
        diff="",
        file_name=file_name,
        intention="test",
        **kw,
    )


def main():
    wd = Path(tempfile.mkdtemp(prefix="fs-perm-wd-"))
    extra = Path(tempfile.mkdtemp(prefix="fs-perm-extra-"))
    # 越界目录不能建在/tmp下（临时目录本身始终放行），用家目录下的临时目录
    outside = Path.home() / ".fs-perm-outside"
    outside.mkdir(exist_ok=True)
    policy = permissions.PathPolicy(wd, [extra])
    handler = permissions.make_fs_permission_handler(policy)
    tmpdir = Path(tempfile.gettempdir()).resolve()

    # PathPolicy基础行为
    assert policy.resolve("rel.txt") == (wd / "rel.txt").resolve()
    assert policy.is_allowed(policy.resolve(str(extra / "x")))
    assert not policy.is_allowed(Path("/etc/passwd").resolve())

    def decision_of(request) -> str:
        result = handler(request, {"session_id": "test"})
        return result.kind

    cases: list[tuple[str, str, str]] = [
        # (描述, 请求, 期望kind)
        ("工作区内写入(绝对路径)", write(str(wd / "a.txt")), "approve-once"),
        ("工作区内写入(相对路径)", write("b.txt"), "approve-once"),
        ("临时目录写入", write(str(tmpdir / "c.txt")), "approve-once"),
        ("额外目录写入", write(str(extra / "d.txt")), "approve-once"),
        ("工作区外写入", write(str(outside / "e.txt")), "reject"),
        (
            "工作区外写入(resolved_path)",
            write("x", resolved_path=str(outside / "f.txt")),
            "reject",
        ),
        (
            "符号链接逃逸",
            write(_symlink_escape(wd, outside)),
            "reject",
        ),
        (
            "工作区内读取",
            PermissionRequestRead(intention="t", path=str(wd / "a.txt")),
            "approve-once",
        ),
        ("工作区外读取", PermissionRequestRead(intention="t", path="/etc/passwd"), "reject"),
        (
            "MCP工具",
            PermissionRequestMcp(read_only=True, server_name="s", tool_name="t", tool_title="T"),
            "approve-once",
        ),
        (
            "自有工具",
            PermissionRequestCustomTool(tool_name="send_image", tool_description="d"),
            "approve-once",
        ),
        ("URL", PermissionRequestUrl(intention="t", url="https://example.com"), "approve-once"),
        ("memory", PermissionRequestMemory(fact="f"), "approve-once"),
        (
            "shell仅工作区路径",
            _shell("cat notes.txt", [str(wd / "notes.txt")]),
            "approve-once",
        ),
        ("shell无路径", _shell("git status", []), "approve-once"),
        (
            "shell越界路径",
            _shell("cat /etc/passwd", ["/etc/passwd"]),
            "reject",
        ),
    ]

    failed = 0
    for desc, request, expected in cases:
        got = decision_of(request)
        mark = "PASS" if got == expected else "FAIL"
        if got != expected:
            failed += 1
        print(f"[{mark}] {desc}: 期望 {expected}, 实际 {got}")

    # 未知类型（模拟新增权限kind）应驳回
    class FakeRequest:
        kind = "future-kind"

    got = decision_of(FakeRequest())
    mark = "PASS" if got == "reject" else "FAIL"
    if got != "reject":
        failed += 1
    print(f"[{mark}] 未知类型fail-closed: 期望 reject, 实际 {got}")

    if failed:
        print(f"\n{failed} 个用例失败")
        shutil.rmtree(outside, ignore_errors=True)
        sys.exit(1)
    print("\n全部用例通过")
    shutil.rmtree(outside, ignore_errors=True)


def _shell(command: str, possible_paths: list[str]) -> PermissionRequestShell:
    return PermissionRequestShell(
        can_offer_session_approval=True,
        commands=[],
        full_command_text=command,
        has_write_file_redirection=False,
        intention="test",
        possible_paths=possible_paths,
        possible_urls=[],
    )


def _symlink_escape(wd: Path, outside: Path) -> str:
    target = outside / "escape.txt"
    target.write_text("x")
    link = wd / "link.txt"
    link.symlink_to(target)
    return str(link)


if __name__ == "__main__":
    main()

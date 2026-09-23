"""受限文件访问的权限处理器

Copilot SDK当前没有沙箱，这里通过自定义`on_permission_request`处理器在审批层
实现路径白名单策略（配合会话配置的`additional_directories`）：

- MCP、bot自有工具（custom-tool）、URL、memory请求：全部放行；
- write / read请求：目标路径必须位于会话工作目录、`additional_directories`
  配置的额外目录或系统临时目录内，否则驳回；
- shell请求：运行时对命令文本做启发式路径提取（possiblePaths），
  任一路径越界即整条命令驳回；
- 未识别的请求类型：一律驳回（fail-closed，避免新类型权限被静默放行）。

注意这不是真正的沙箱：shell路径提取是启发式的，复杂命令中的越界路径可能
不被识别；若启用了`bash`等shell工具，仍存在绕过风险，建议在配置的
`excluded_tools`中排除bash类工具。
"""

import tempfile
from collections.abc import Iterable
from pathlib import Path

from copilot import PermissionRequest, PermissionRequestResult
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session import PermissionInvocation
from copilot.session_events import (
    PermissionRequestCustomTool,
    PermissionRequestMcp,
    PermissionRequestMemory,
    PermissionRequestRead,
    PermissionRequestShell,
    PermissionRequestUrl,
    PermissionRequestWrite,
)
from nonebot import logger

_REJECT_FEEDBACK = "文件访问被限制在工作目录、额外允许目录和系统临时目录内，请仅在允许范围内操作"


class PathPolicy:
    """路径白名单策略

    允许目录 = 会话工作目录 + 额外目录 + 系统临时目录。
    同时供权限处理器和send_text_file等宿主工具使用。
    """

    def __init__(self, working_directory: Path, additional_directories: Iterable[Path] = ()):
        def normalize(path: Path) -> Path:
            return path.expanduser().resolve()

        self.working_directory = normalize(working_directory)
        roots = [self.working_directory]
        roots += [normalize(p) for p in additional_directories]
        roots.append(Path(tempfile.gettempdir()).resolve())
        # 去重保序的允许目录根列表
        self.allowed_roots = list(dict.fromkeys(roots))

    def resolve(self, raw: str) -> Path:
        """把路径解析为绝对实路径，相对路径按工作目录解析"""
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.working_directory / p
        return p.resolve()

    def is_allowed(self, target: Path) -> bool:
        return any(target == root or root in target.parents for root in self.allowed_roots)


def make_fs_permission_handler(policy: PathPolicy):
    """构建限制文件访问范围的权限处理器

    Args:
        policy: 路径白名单策略（工作目录、额外目录与系统临时目录始终允许）
    """
    allowed_roots = policy.allowed_roots

    def reject(reason: str) -> PermissionDecisionReject:
        logger.warning(f"权限驳回: {reason}；允许的目录: {allowed_roots}")
        return PermissionDecisionReject(feedback=f"{reason}。{_REJECT_FEEDBACK}")

    def handler(
        request: PermissionRequest, invocation: PermissionInvocation
    ) -> PermissionRequestResult:
        # MCP工具全部放行；bot自有工具参数由宿主代码约束，同样放行
        if isinstance(
            request,
            PermissionRequestMcp
            | PermissionRequestCustomTool
            | PermissionRequestUrl
            | PermissionRequestMemory,
        ):
            return PermissionDecisionApproveOnce()

        if isinstance(request, PermissionRequestWrite):
            raw = request.resolved_path or request.file_name
            if policy.is_allowed(policy.resolve(raw)):
                return PermissionDecisionApproveOnce()
            return reject(f"拒绝写入: {raw}")

        if isinstance(request, PermissionRequestRead):
            raw = request.resolved_path or request.path
            if policy.is_allowed(policy.resolve(raw)):
                return PermissionDecisionApproveOnce()
            return reject(f"拒绝读取: {raw}")

        if isinstance(request, PermissionRequestShell):
            # possible_paths为运行时对命令文本启发式提取出的路径
            resolved = request.resolved_paths or {}
            for raw in request.possible_paths:
                if not policy.is_allowed(policy.resolve(resolved.get(raw, raw))):
                    return reject(f"命令涉及越界路径 {raw}: {request.full_command_text}")
            return PermissionDecisionApproveOnce()

        # 未知类型一律驳回，避免新的权限类型被静默放行
        return reject(f"未放行的权限类型: {getattr(request, 'kind', type(request).__name__)}")

    return handler

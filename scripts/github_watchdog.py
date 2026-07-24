#!/usr/bin/env python3
import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path

from httpx import AsyncClient, HTTPStatusError, RequestError, Timeout
from loguru import logger
from pydantic import BaseModel

from scripts.util import get_config

ROOT_DIR = Path(__file__).resolve().parent


class ScopedConfig(BaseModel):
    """Watchdog 配置模型，用于描述服务启动所需的环境参数。"""

    github_repo: str = "njdldkl666699/kanade-bot"
    """GitHub 仓库，格式为 owner/repo"""
    github_branch: str = "main"
    """GitHub 分支名称"""
    github_token: str = ""
    """GitHub 访问令牌，建议使用具有 repo 访问权限的个人访问令牌（PAT），以避免 API 速率限制"""
    poll_interval: int = 30
    """轮询 GitHub 仓库更新的时间间隔，单位为秒，必须大于 0"""
    core_command: str = "uv sync && nb run"
    """要作为 core process 启动的命令，可包含参数"""
    user_agent: str = "kanade-watchdog"
    """User-Agent 字段，用于标识请求来源"""


class Config(BaseModel):
    watchdog: ScopedConfig


def _build_github_headers(token: str, user_agent: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _run_command(*args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(ROOT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = await process.communicate()
    text = ""
    if output[0]:
        text = output[0].decode("utf-8", errors="replace").strip()
    return process.returncode or 0, text


async def _wait_for_process_exit(
    process: asyncio.subprocess.Process,
    timeout: float,
) -> bool:
    if process.returncode is not None:
        return True

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _wait_for_shutdown(event: asyncio.Event, timeout: int) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        return


class GitRepository:
    async def get_current_sha(self) -> str | None:
        return_code, output = await _run_command("git", "rev-parse", "HEAD")
        if return_code != 0:
            logger.error("获取本地提交 SHA 失败")
            return None

        if not output:
            logger.error("本地提交 SHA 为空")
            return None

        return output.splitlines()[0].strip()

    async def pull_latest(self) -> bool:
        return_code, output = await _run_command("git", "pull", "--ff-only")
        if output:
            logger.info("git pull 输出：\n{}", output)
        if return_code != 0:
            logger.error("git pull 执行失败，退出码：{}", return_code)
            return False

        return True


class GitHubClient:
    def __init__(self, config: ScopedConfig, client: AsyncClient) -> None:
        self._config = config
        self._client = client

    async def fetch_latest_commit_sha(self) -> str | None:
        if not self._config.github_repo:
            logger.error("WATCHDOG__GITHUB_REPO 不能为空")
            return None

        url = (
            f"https://api.github.com/repos/{self._config.github_repo}"
            f"/commits/{self._config.github_branch}"
        )
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except HTTPStatusError as exc:
            logger.warning("GitHub API 返回错误状态码：{}", exc.response.status_code)
            return None
        except RequestError as exc:
            logger.warning("请求 GitHub API 失败：{}", exc)
            return None

        data = response.json()
        sha = data.get("sha")
        if not isinstance(sha, str):
            logger.warning("GitHub API 响应格式异常：{}", json.dumps(data, ensure_ascii=False))
            return None
        return sha


class CoreProcessManager:
    def __init__(self, command: str) -> None:
        self._command = command
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        logger.info("正在启动 core 进程：{}", self._command)
        if os.name == "nt":
            self._process = await asyncio.create_subprocess_shell(
                self._command,
                cwd=str(ROOT_DIR),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            return

        self._process = await asyncio.create_subprocess_shell(
            self._command,
            cwd=str(ROOT_DIR),
            start_new_session=True,
        )

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def ensure_running(self) -> None:
        if self._process is None:
            await self.start()
            return

        if self._process.returncode is None:
            return

        logger.warning("core 进程已退出，退出码：{}", self._process.returncode)
        await self.stop()
        await self.start()

    async def stop(self) -> None:
        if self._process is None:
            return

        if os.name == "nt":
            await self._stop_windows_process_tree(self._process)
        else:
            await self._stop_posix_process_group(self._process)
        self._process = None

    async def _stop_posix_process_group(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        pgid = self._get_process_group_id(process)
        logger.info("正在停止 core 进程组（pid={}，pgid={}）", process.pid, pgid)
        self._send_process_group_signal(pgid, signal.SIGTERM)

        if await self._wait_for_process_group_exit(process, pgid, timeout=90):
            return

        logger.warning("core 进程组未在规定时间内退出，准备强制结束")
        self._send_process_group_signal(pgid, signal.SIGKILL)

        if not await self._wait_for_process_group_exit(process, pgid, timeout=30):
            logger.warning("core 进程组可能仍有残留进程")

    async def _stop_windows_process_tree(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        logger.info("正在停止 core 进程树（pid={}）", process.pid)

        if process.returncode is not None:
            return

        await self._taskkill_process_tree(process.pid, force=False)
        if await _wait_for_process_exit(process, timeout=90):
            return

        logger.warning("core 进程树未在规定时间内退出，准备强制结束")
        await self._taskkill_process_tree(process.pid, force=True)

        if not await _wait_for_process_exit(process, timeout=30):
            logger.warning("core 进程树可能仍有残留进程")

    async def _taskkill_process_tree(self, pid: int, force: bool) -> None:
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output = await process.communicate()
        text = ""
        if output[0]:
            text = output[0].decode("utf-8", errors="replace").strip()

        if text:
            logger.info("taskkill 输出：\n{}", text)
        if process.returncode not in (0, None):
            logger.warning("taskkill 执行结束，退出码：{}", process.returncode)

    @staticmethod
    def _get_process_group_id(process: asyncio.subprocess.Process) -> int:
        try:
            return os.getpgid(process.pid)
        except ProcessLookupError:
            return process.pid

    @staticmethod
    def _send_process_group_signal(pgid: int, sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return False
        return True

    def _process_group_exists(self, pgid: int) -> bool:
        return self._send_process_group_signal(pgid, 0)

    async def _wait_for_process_group_exit(
        self,
        process: asyncio.subprocess.Process,
        pgid: int,
        timeout: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            process_exited = process.returncode is not None
            group_exited = not self._process_group_exists(pgid)
            if process_exited and group_exited:
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False

            step = min(0.2, remaining)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=step)
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(step)


class WatchdogService:
    def __init__(self, config: ScopedConfig) -> None:
        self._config = config
        self._git = GitRepository()
        self._core = CoreProcessManager(config.core_command)
        self._last_reported_sha: str | None = None

    async def run(self) -> None:
        if not self._validate_config():
            return

        logger.info(
            "Watchdog 正在轮询 GitHub 仓库 '{}' 的 '{}' 分支，间隔 {} 秒",
            self._config.github_repo,
            self._config.github_branch,
            self._config.poll_interval,
        )

        shutdown_event = self._create_shutdown_event()
        timeout = Timeout(10.0)
        headers = _build_github_headers(self._config.github_token, self._config.user_agent)

        await self._core.start()
        try:
            async with AsyncClient(timeout=timeout, headers=headers) as http_client:
                github = GitHubClient(self._config, http_client)
                await self._poll_loop(github, shutdown_event)
        finally:
            await self._core.stop()

    def _validate_config(self) -> bool:
        if self._config.poll_interval <= 0:
            logger.error("WATCHDOG__POLL_INTERVAL 必须大于 0")
            return False

        if not self._config.core_command.strip():
            logger.error("WATCHDOG__CORE_COMMAND 不能为空")
            return False

        return True

    def _create_shutdown_event(self) -> asyncio.Event:
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request_shutdown(signal_name: str) -> None:
            logger.info("收到 {}，准备关闭", signal_name)
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_shutdown, sig.name)
            except NotImplementedError:
                signal.signal(
                    sig,
                    lambda _signum, _frame, sig=sig: loop.call_soon_threadsafe(
                        request_shutdown,
                        sig.name,
                    ),
                )

        return shutdown_event

    async def _poll_loop(
        self,
        github: GitHubClient,
        shutdown_event: asyncio.Event,
    ) -> None:
        while not shutdown_event.is_set():
            await self._core.ensure_running()
            await self._check_once(github)
            await _wait_for_shutdown(shutdown_event, self._config.poll_interval)

    async def _check_once(self, github: GitHubClient) -> None:
        remote_sha = await github.fetch_latest_commit_sha()
        if not remote_sha:
            logger.warning("获取远端最新提交 SHA 失败")
            return

        local_sha = await self._git.get_current_sha()
        if not local_sha:
            return

        await self._handle_commit_update(local_sha, remote_sha)

    async def _handle_commit_update(self, local_sha: str, remote_sha: str) -> None:
        if remote_sha == local_sha:
            if self._last_reported_sha != remote_sha:
                logger.info("当前提交：{}", remote_sha)
            self._last_reported_sha = remote_sha
            return

        if self._last_reported_sha != remote_sha:
            logger.info("发现新提交：{}（本地：{}）", remote_sha, local_sha)

        if await self._git.pull_latest():
            await self._core.restart()
        self._last_reported_sha = remote_sha


async def main() -> None:
    cfg = get_config(Config).watchdog
    await WatchdogService(cfg).run()


if __name__ == "__main__":
    asyncio.run(main())

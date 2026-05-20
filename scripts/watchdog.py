import asyncio
import json
import os
from pathlib import Path
import signal

from httpx import AsyncClient, HTTPStatusError, RequestError, Timeout
from loguru import logger
from pydantic import BaseModel

from .util import get_config

ROOT_DIR = Path(__file__).resolve().parents[1]


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


class Config(BaseModel):
    watchdog: ScopedConfig


cfg = get_config(Config).watchdog


def _build_github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "kanade-watchdog",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_latest_commit_sha(client: AsyncClient) -> str | None:
    if not cfg.github_repo:
        logger.error("WATCHDOG_GITHUB_REPO is empty")
        return None

    url = f"https://api.github.com/repos/{cfg.github_repo}/commits/{cfg.github_branch}"
    try:
        response = await client.get(url)
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.warning("GitHub API error: {}", exc.response.status_code)
        return None
    except RequestError as exc:
        logger.warning("GitHub API request failed: {}", exc)
        return None

    data = response.json()
    sha = data.get("sha")
    if not isinstance(sha, str):
        logger.warning("Unexpected GitHub API response: {}", json.dumps(data))
        return None
    return sha


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


async def _get_local_commit_sha() -> str | None:
    return_code, output = await _run_command("git", "rev-parse", "HEAD")
    if return_code != 0:
        logger.error("Failed to get local commit SHA")
        return None

    if not output:
        logger.error("Local commit SHA is empty")
        return None

    return output.splitlines()[0].strip()


async def _start_core_process() -> asyncio.subprocess.Process:
    logger.info("Starting core process with 'nb run'")
    return await asyncio.create_subprocess_exec(
        "nb",
        "run",
        cwd=str(ROOT_DIR),
        start_new_session=True,
    )


def _get_core_process_group_id(process: asyncio.subprocess.Process) -> int:
    try:
        return os.getpgid(process.pid)
    except ProcessLookupError:
        return process.pid


def _send_core_process_group_signal(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _core_process_group_exists(pgid: int) -> bool:
    return _send_core_process_group_signal(pgid, 0)


async def _wait_for_core_process_exit(
    process: asyncio.subprocess.Process,
    pgid: int,
    timeout: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        process_exited = process.returncode is not None
        group_exited = not _core_process_group_exists(pgid)
        if process_exited and group_exited:
            return True

        remaining = deadline - loop.time()
        if remaining <= 0:
            return False

        step = min(0.2, remaining)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=step)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(step)


async def _stop_core_process(process: asyncio.subprocess.Process) -> None:
    pgid = _get_core_process_group_id(process)
    logger.info("Stopping core process group (pid={}, pgid={})", process.pid, pgid)
    _send_core_process_group_signal(pgid, signal.SIGTERM)

    if await _wait_for_core_process_exit(process, pgid, timeout=90):
        return

    logger.warning("Core process did not exit in time; killing")
    _send_core_process_group_signal(pgid, signal.SIGKILL)

    if not await _wait_for_core_process_exit(process, pgid, timeout=30):
        logger.warning("Core process group may still have leftover processes")


async def _restart_core_process(
    process: asyncio.subprocess.Process,
) -> asyncio.subprocess.Process:
    await _stop_core_process(process)
    return await _start_core_process()


async def _ensure_core_running(
    process: asyncio.subprocess.Process,
) -> asyncio.subprocess.Process:
    if process.returncode is None:
        return process

    logger.warning("Core process exited with code {}", process.returncode)
    await _stop_core_process(process)
    return await _start_core_process()


async def _pull_and_sync() -> bool:
    return_code, output = await _run_command("git", "pull", "--ff-only")
    if output:
        logger.info("git pull output:\n{}", output)
    if return_code != 0:
        logger.error("git pull failed with code {}", return_code)
        return False

    sync_code, sync_output = await _run_command("uv", "sync")
    if sync_output:
        logger.info("uv sync output:\n{}", sync_output)
    if sync_code != 0:
        logger.error("uv sync failed with code {}", sync_code)

    return True


async def _handle_commit_update(
    core_process: asyncio.subprocess.Process,
    last_reported_sha: str | None,
    local_sha: str,
    remote_sha: str,
) -> tuple[asyncio.subprocess.Process, str | None]:
    if remote_sha == local_sha:
        if last_reported_sha != remote_sha:
            logger.info("Current commit: {}", remote_sha)
        return core_process, remote_sha

    if last_reported_sha != remote_sha:
        logger.info("New commit detected: {} (local {})", remote_sha, local_sha)

    if await _pull_and_sync():
        core_process = await _restart_core_process(core_process)
    return core_process, remote_sha


async def _wait_for_shutdown(event: asyncio.Event, timeout: int) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return


async def main() -> None:
    if cfg.poll_interval <= 0:
        logger.error("WATCHDOG_POLL_INTERVAL must be greater than 0")
        return

    logger.info(
        "Watchdog polling GitHub repo '{}' on branch '{}' every {}s",
        cfg.github_repo,
        cfg.github_branch,
        cfg.poll_interval,
    )

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signal_name: str) -> None:
        logger.info("Received {}, shutting down", signal_name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig.name)

    last_reported_sha: str | None = None
    timeout = Timeout(10.0)
    headers = _build_github_headers(cfg.github_token)
    core_process = await _start_core_process()
    try:
        async with AsyncClient(timeout=timeout, headers=headers) as client:
            while not shutdown_event.is_set():
                core_process = await _ensure_core_running(core_process)
                remote_sha = await _fetch_latest_commit_sha(client)
                if not remote_sha:
                    logger.warning("Failed to fetch latest commit SHA")
                    await _wait_for_shutdown(shutdown_event, cfg.poll_interval)
                    continue

                local_sha = await _get_local_commit_sha()
                if not local_sha:
                    await _wait_for_shutdown(shutdown_event, cfg.poll_interval)
                    continue

                core_process, last_reported_sha = await _handle_commit_update(
                    core_process,
                    last_reported_sha,
                    local_sha,
                    remote_sha,
                )
                await _wait_for_shutdown(shutdown_event, cfg.poll_interval)
    finally:
        await _stop_core_process(core_process)


if __name__ == "__main__":
    asyncio.run(main())

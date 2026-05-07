import asyncio
import json
import os
import signal
from pathlib import Path

from dotenv import dotenv_values
from httpx import AsyncClient, HTTPStatusError, RequestError, Timeout
from loguru import logger
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[1]


def _read_env_values() -> dict[str, str | None]:
    base_env = dotenv_values(ROOT_DIR / ".env")
    environment = (base_env.get("ENVIRONMENT") or os.getenv("ENVIRONMENT") or "").strip()
    env_values: dict[str, str | None] = {}
    if environment:
        env_path = ROOT_DIR / f".env.{environment}"
        env_values = dotenv_values(env_path)

    # Merge base and environment values, env-specific overrides base.
    return {**base_env, **env_values}


class WatchdogConfig(BaseModel):
    """Watchdog 配置模型，用于描述服务启动所需的环境参数。"""

    github_repo: str = Field(default="", description="GitHub 仓库，格式为 owner/repo")
    github_branch: str = Field(default="main", description="监听的分支名称")
    github_token: str = Field(default="", description="GitHub 访问令牌（可选）")
    poll_interval: int = Field(default=30, description="轮询间隔（秒）")


def _load_watchdog_config() -> WatchdogConfig:
    values = _read_env_values()
    return WatchdogConfig(
        github_repo=values.get("WATCHDOG_GITHUB_REPO") or "",
        github_branch=values.get("WATCHDOG_GITHUB_BRANCH") or "main",
        github_token=values.get("WATCHDOG_GITHUB_TOKEN") or "",
        poll_interval=int(values.get("WATCHDOG_POLL_INTERVAL") or 30),
    )


WATCHDOG_CONFIG = _load_watchdog_config()


def _build_github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "kanade-watchdog",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_latest_commit_sha(client: AsyncClient, config: WatchdogConfig) -> str | None:
    if not config.github_repo:
        logger.error("WATCHDOG_GITHUB_REPO is empty")
        return None

    url = f"https://api.github.com/repos/{config.github_repo}/commits/{config.github_branch}"
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


async def _start_core_process() -> asyncio.subprocess.Process:
    logger.info("Starting core process with 'nb run'")
    return await asyncio.create_subprocess_shell(
        "nb run",
        cwd=str(ROOT_DIR),
    )


async def _stop_core_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    logger.info("Stopping core process (pid={})", process.pid)
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("Core process did not exit in time; killing")
        process.kill()
        await process.wait()


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
    last_sha: str | None,
    sha: str,
) -> tuple[asyncio.subprocess.Process, str | None]:
    if last_sha is None:
        logger.info("Current commit: {}", sha)
        return core_process, sha

    if sha == last_sha:
        return core_process, last_sha

    logger.info("New commit detected: {}", sha)
    if await _pull_and_sync():
        core_process = await _restart_core_process(core_process)
    return core_process, sha


async def _wait_for_shutdown(event: asyncio.Event, timeout: int) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return


async def main() -> None:
    if WATCHDOG_CONFIG.poll_interval <= 0:
        logger.error("WATCHDOG_POLL_INTERVAL must be greater than 0")
        return

    logger.info(
        "Watchdog polling GitHub repo '{}' on branch '{}' every {}s",
        WATCHDOG_CONFIG.github_repo,
        WATCHDOG_CONFIG.github_branch,
        WATCHDOG_CONFIG.poll_interval,
    )

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signal_name: str) -> None:
        logger.info("Received {}, shutting down", signal_name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            signal.signal(
                sig,
                lambda _signum, _frame, s=sig: loop.call_soon_threadsafe(_request_shutdown, s.name),
            )

    last_sha: str | None = None
    timeout = Timeout(10.0)
    headers = _build_github_headers(WATCHDOG_CONFIG.github_token)
    core_process = await _start_core_process()
    try:
        async with AsyncClient(timeout=timeout, headers=headers) as client:
            while not shutdown_event.is_set():
                core_process = await _ensure_core_running(core_process)
                sha = await _fetch_latest_commit_sha(client, WATCHDOG_CONFIG)
                if not sha:
                    logger.warning("Failed to fetch latest commit SHA")
                    continue

                core_process, last_sha = await _handle_commit_update(core_process, last_sha, sha)
                await _wait_for_shutdown(shutdown_event, WATCHDOG_CONFIG.poll_interval)
    finally:
        await _stop_core_process(core_process)


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import json
import os
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


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class WatchdogConfig(BaseModel):
    """Watchdog 配置模型，用于描述服务启动所需的环境参数。"""

    enabled: bool = Field(default=False, description="是否启用 Watchdog 服务")
    github_repo: str = Field(default="", description="GitHub 仓库，格式为 owner/repo")
    github_branch: str = Field(default="main", description="监听的分支名称")
    github_token: str = Field(default="", description="GitHub 访问令牌（可选）")
    poll_interval: int = Field(default=30, description="轮询间隔（秒）")


def _load_watchdog_config() -> WatchdogConfig:
    values = _read_env_values()
    return WatchdogConfig(
        enabled=_parse_bool(values.get("WATCHDOG_ENABLED"), default=False),
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


async def main() -> None:
    if not WATCHDOG_CONFIG.enabled:
        logger.error("Watchdog disabled (WATCHDOG_ENABLED=false)")
        return

    if WATCHDOG_CONFIG.poll_interval <= 0:
        logger.error("WATCHDOG_POLL_INTERVAL must be greater than 0")
        return

    logger.info(
        "Watchdog polling GitHub repo '{}' on branch '{}' every {}s",
        WATCHDOG_CONFIG.github_repo,
        WATCHDOG_CONFIG.github_branch,
        WATCHDOG_CONFIG.poll_interval,
    )

    last_sha: str | None = None
    timeout = Timeout(10.0)
    headers = _build_github_headers(WATCHDOG_CONFIG.github_token)
    core_process = await _start_core_process()
    async with AsyncClient(timeout=timeout, headers=headers) as client:
        while True:
            if core_process.returncode is not None:
                logger.warning("Core process exited with code {}", core_process.returncode)
                core_process = await _start_core_process()

            sha = await _fetch_latest_commit_sha(client, WATCHDOG_CONFIG)
            if sha:
                if last_sha is None:
                    logger.info("Current commit: {}", sha)
                elif sha != last_sha:
                    logger.info("New commit detected: {}", sha)
                    return_code, output = await _run_command("git", "pull", "--ff-only")
                    if output:
                        logger.info("git pull output:\n{}", output)
                    if return_code == 0:
                        await _stop_core_process(core_process)
                        core_process = await _start_core_process()
                    else:
                        logger.error("git pull failed with code {}", return_code)
                last_sha = sha

            await asyncio.sleep(WATCHDOG_CONFIG.poll_interval)


if __name__ == "__main__":
    asyncio.run(main())

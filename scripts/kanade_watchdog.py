import hmac
import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, Header, HTTPException, Request, status
from loguru import logger

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


def _load_watchdog_config() -> dict[str, Any]:
    values = _read_env_values()
    return {
        "enabled": _parse_bool(values.get("WATCHDOG_ENABLED"), default=False),
        "port": int(values.get("WATCHDOG_PORT") or 8000),
        "secret": values.get("WATCHDOG_SECRET") or "",
    }


WATCHDOG_CONFIG = _load_watchdog_config()

app = FastAPI(title="Kanade Watchdog")


def _verify_github_signature(payload: bytes, signature: str | None, secret: str) -> None:
    if not secret:
        logger.warning("WATCHDOG_SECRET is empty, rejecting webhook")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Secret missing")
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")

    try:
        scheme, digest = signature.split("=", 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature format"
        ) from exc

    if scheme != "sha256":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unsupported signature scheme"
        )

    expected = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad signature")


@app.post("/hook/push")
async def hook_push(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    payload = await request.body()
    _verify_github_signature(payload, x_hub_signature_256, WATCHDOG_CONFIG["secret"])

    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        data = {"raw": payload.decode("utf-8", errors="replace")}

    logger.info("GitHub webhook event: {}", x_github_event or "unknown")
    logger.info("Webhook payload: {}", json.dumps(data, ensure_ascii=False))
    return {"ok": True}


def main() -> None:
    if not WATCHDOG_CONFIG["enabled"]:
        logger.info("Watchdog disabled (WATCHDOG_ENABLED=false)")
        return

    port = WATCHDOG_CONFIG["port"]
    logger.info("Starting watchdog on 0.0.0.0:{}", port)
    uvicorn.run(app, host="::", port=port)


if __name__ == "__main__":
    main()

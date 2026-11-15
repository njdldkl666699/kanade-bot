"""冒烟：TTS插件（反向WebSocket网关 + 命令参数解析）

进程内启动 uvicorn 承载 nonebot 的 FastAPI app，用 websockets 客户端模拟
GPT-SoVITS 推理节点拨号，覆盖协议主路径：
注册握手 / 合成成功（含stream轨过滤）/ 拒绝 / 语言与长度预检 / 断线作废 / 超时取消 / 鉴权。
另覆盖 send_voice 工具的情绪回退与 /tts 命令的参数解析。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot import init_nonebot, register_adapters_and_load_plugins
from scripts.util import load_configs

# 复用生产初始化路径（不 run 不监听；model_updater / tts 插件导入期需要 ASGI driver）
_env, _configs = load_configs(Path(__file__).parents[2])
init_nonebot(env=_env, **_configs)
register_adapters_and_load_plugins()

import uvicorn
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from kanade_bot.plugins.chat.agent.tools.messaging import send_voice
from kanade_bot.plugins.tts import gateway as tts_gateway
from kanade_bot.plugins.tts.config import (
    TTS_DEFAULT_EMOTION,
    TTS_EMOTIONS,
    Emotion,
    TTSConfigs,
    tts_configs,
)
from kanade_bot.plugins.tts.gateway import V2_LANGUAGES, TTSError, synthesize
from kanade_bot.plugins.tts.handler import parse_tts_args

HELLO = {
    "type": "node.hello",
    "ts": 0,
    "payload": {
        "node_id": "test-node",
        "protocol": "tts-ws/1.0",
        "capabilities": {
            "version": "v2",
            "languages": ["zh", "ja", "en"],
            "media_types": ["wav", "raw", "ogg", "aac"],
            "streaming_modes": [0, 1, 2, 3],
            "max_concurrency": 1,
            "max_text_chars": 1000,
            "native_sample_rate": 32000,
        },
        "models": {"gpt": "g.pth", "sovits": "s.pth", "device": "cpu"},
    },
}

FAKE_WAV = b"RIFF-fake-wav-" + b"\x00" * 64


def frame(mtype: str, payload: dict | None = None, rid: str | None = None) -> str:
    f: dict = {"type": mtype, "ts": int(time.time() * 1000)}
    if rid:
        f["id"] = rid
    f["payload"] = payload or {}
    return json.dumps(f)


def audio_frame(rid: str, data: bytes) -> bytes:
    ridb = rid.encode()
    return bytes([len(ridb)]) + ridb + data


async def recv_json(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def recv_request(ws) -> dict:
    """跳过无关帧直到收到tts.request"""
    while True:
        msg = await recv_json(ws)
        if msg.get("type") == "tts.request":
            return msg


async def node_reply_ok(ws, rid: str, *, stream_track: bool = False) -> None:
    """节点按协议回复一次成功合成；stream_track=True时先发一条stream轨验证过滤"""
    await ws.send(frame("tts.accept", {}, rid))
    track = 1
    if stream_track:
        await ws.send(
            frame(
                "tts.audio.begin",
                {"track": track, "role": "stream", "format": "pcm", "sample_rate": 32000},
                rid,
            )
        )
        await ws.send(audio_frame(rid, b"STREAM-PCM-SHOULD-NOT-COLLECT"))
        await ws.send(frame("tts.audio.end", {"track": track, "bytes": 27}, rid))
        track += 1
    await ws.send(
        frame(
            "tts.audio.begin",
            {"track": track, "role": "final", "format": "wav", "sample_rate": 32000},
            rid,
        )
    )
    await ws.send(audio_frame(rid, FAKE_WAV))
    await ws.send(frame("tts.audio.end", {"track": track, "bytes": len(FAKE_WAV)}, rid))
    await ws.send(frame("tts.done", {"duration_s": 1.0, "audio_bytes": len(FAKE_WAV)}, rid))


async def start_server(app) -> tuple[uvicorn.Server, asyncio.Task, int]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("uvicorn 启动超时")
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


async def expect_tts_error(coro) -> TTSError:
    try:
        await coro
    except TTSError as e:
        return e
    raise AssertionError("预期抛出TTSError")


async def main():
    server, serve_task, port = await start_server(tts_gateway.app)
    url = f"ws://127.0.0.1:{port}{tts_gateway.WS_PATH}"
    orig_timeout = tts_gateway.cfg.request_timeout_s
    orig_token = tts_gateway.cfg.auth_token
    try:
        # ── 场景A：注册握手 + 合成成功（含stream轨过滤） ──
        async with connect(url) as ws:
            await ws.send(json.dumps(HELLO))
            welcome = await recv_json(ws)
            assert welcome["type"] == "gateway.welcome", welcome
            assert welcome["payload"]["heartbeat_interval_s"] == 20
            assert tts_gateway.node_connected()

            task = asyncio.create_task(
                synthesize(
                    "こんにちは",
                    "ja",
                    ref_audio_path="/dataset/ref.wav",
                    prompt_text="なんだか、タイトルからそれらしくていいね",
                    prompt_lang="ja",
                )
            )
            req = await recv_request(ws)
            assert req["id"].startswith("r-"), req
            p = req["payload"]
            assert p["text"] == "こんにちは" and p["text_lang"] == "ja"
            assert p["ref_audio_path"] == "/dataset/ref.wav"
            assert p["prompt_text"] == "なんだか、タイトルからそれらしくていいね"
            assert p["media_type"] == "wav" and p["streaming_mode"] == 0
            assert p["seed"] == tts_gateway.cfg.seed
            await node_reply_ok(ws, req["id"], stream_track=True)
            audio = await task
            assert audio == FAKE_WAV, audio[:32]  # stream轨字节未混入

            # ── 场景A2：send_voice全链路 —— 未配置情绪回退平静 + 语言小写化 ──
            calm = tts_configs.instance.tts_emotions[TTS_DEFAULT_EMOTION]
            ctx = SimpleNamespace(
                deps=SimpleNamespace(
                    bot_id="nonexistent", session_info=SimpleNamespace(session_id="test")
                )
            )
            task = asyncio.create_task(send_voice(ctx, "こんにちは", "快乐", "JA"))  # type: ignore[arg-type]
            req = await recv_request(ws)
            assert (
                req["payload"]["ref_audio_path"] == calm.ref_audio_path
            )  # 「快乐」缺项→回退「平静」
            assert req["payload"]["text_lang"] == "ja"  # 大写已归一
            await node_reply_ok(ws, req["id"])
            ret = await task
            assert "无法获取Bot实例" in ret, ret  # 合成成功，仅发送侧报错

            # ── 场景B：节点拒绝（invalid_params） ──
            task = asyncio.create_task(
                synthesize("text", "ja", ref_audio_path="/r.wav", prompt_text="t", prompt_lang="ja")
            )
            req = await recv_request(ws)
            await ws.send(
                frame(
                    "tts.reject",
                    {"code": 1004, "message": "text_lang must be lowercase"},
                    req["id"],
                )
            )
            e = await expect_tts_error(task)
            assert e.code == 1004 and "lowercase" in e.message, e

            # ── 场景C：网关侧语言/长度预检（不达节点） ──
            e = await expect_tts_error(
                synthesize("text", "fr", ref_audio_path="/r.wav", prompt_text="t", prompt_lang="ja")
            )
            assert e.code == 2002, e
            assert "fr" in e.message
            e = await expect_tts_error(
                synthesize(
                    "字" * 1001, "ja", ref_audio_path="/r.wav", prompt_text="t", prompt_lang="ja"
                )
            )
            assert e.code == 2003, e

            # ── 场景D：节点断线 → 在途请求作废3001 ──
            task = asyncio.create_task(
                synthesize("text", "ja", ref_audio_path="/r.wav", prompt_text="t", prompt_lang="ja")
            )
            req = await recv_request(ws)
            assert req["payload"]["text"] == "text"
            await ws.close()  # 不回复直接断开
            e = await expect_tts_error(task)
            assert e.code == 3001, e
        assert not tts_gateway.node_connected()

        # ── 场景E：超时 → 3002 且节点收到tts.cancel ──
        tts_gateway.cfg.request_timeout_s = 0.5
        async with connect(url) as ws:
            await ws.send(json.dumps(HELLO))
            await recv_json(ws)  # welcome
            task = asyncio.create_task(
                synthesize("text", "ja", ref_audio_path="/r.wav", prompt_text="t", prompt_lang="ja")
            )
            await recv_request(ws)  # 收下请求但不回复
            e = await expect_tts_error(task)
            assert e.code == 3002, e
            cancel = await recv_json(ws)
            assert cancel["type"] == "tts.cancel", cancel
        tts_gateway.cfg.request_timeout_s = orig_timeout

        # ── 场景F：鉴权 —— 无token拒绝握手，Bearer放行 ──
        tts_gateway.cfg.auth_token = "secret"
        try:
            async with connect(url):
                raise AssertionError("无token不应握手成功")
        except InvalidStatus as e:
            assert e.response.status_code == 403, e.response.status_code
        async with connect(url, additional_headers={"Authorization": "Bearer secret"}) as ws:
            await ws.send(json.dumps(HELLO))
            welcome = await recv_json(ws)
            assert welcome["type"] == "gateway.welcome"
            task = asyncio.create_task(
                synthesize("text", "en", ref_audio_path="/r.wav", prompt_text="t", prompt_lang="en")
            )
            req = await recv_request(ws)
            await node_reply_ok(ws, req["id"])
            assert await task == FAKE_WAV
        tts_gateway.cfg.auth_token = orig_token

        # ── 场景G：情绪枚举固定 + 配置校验 ──
        emotions = tts_configs.instance.tts_emotions
        assert set(emotions) <= set(TTS_EMOTIONS) and TTS_DEFAULT_EMOTION in emotions
        assert get_args(Emotion) == TTS_EMOTIONS
        assert inspect.signature(send_voice).parameters["emotion"].default == TTS_DEFAULT_EMOTION
        assert inspect.signature(send_voice).parameters["text_lang"].default == "auto"
        calm = emotions[TTS_DEFAULT_EMOTION]
        TTSConfigs()  # 空=未启用TTS，合法
        TTSConfigs(tts_emotions={TTS_DEFAULT_EMOTION: calm, "快乐": calm})  # 含平静，合法
        for bad in ({"快乐": calm}, {"激动": calm}):  # 缺平静 / 未知情绪键
            try:
                TTSConfigs(tts_emotions=bad)
                raise AssertionError(f"应校验失败: {bad}")
            except ValidationError:
                pass

        # ── 场景H：完全未配置时工具直接报错；空文本拦截 ──
        tts_configs.instance.tts_emotions = {}
        try:
            ret = await send_voice(ctx, "hi", "快乐", "ja")  # type: ignore[arg-type]
            assert ret == "未配置任何语音情绪映射，无法合成语音。", ret
        finally:
            tts_configs.instance.tts_emotions = emotions
        ret = await send_voice(ctx, "  ", "平静", "ja")  # type: ignore[arg-type]
        assert ret == "文本为空，无法合成语音。", ret

        # ── 场景I：/tts 命令参数解析 ──
        assert V2_LANGUAGES[:3] == ["auto", "auto_yue", "en"] and "ko" in V2_LANGUAGES
        assert parse_tts_args("ja こんにちは") == ("ja", "こんにちは")
        assert parse_tts_args("JA  こんにちは") == ("ja", "こんにちは")  # 大写归一 + 多空白
        assert parse_tts_args("all_zh 你好 世界") == ("all_zh", "你好 世界")
        assert parse_tts_args("你好 ja 世界") == ("auto", "你好 ja 世界")  # 首段非语言id
        assert parse_tts_args("  こんにちは  ") == ("auto", "こんにちは")
        assert parse_tts_args("ja") == ("auto", "ja")  # 仅语言id无文本时视作普通文本

        print("ALL OK")
    finally:
        tts_gateway.cfg.request_timeout_s = orig_timeout
        tts_gateway.cfg.auth_token = orig_token
        server.should_exit = True
        await serve_task


if __name__ == "__main__":
    asyncio.run(main())

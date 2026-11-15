import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from nonebot import get_app, logger

from .config import cfg

app: FastAPI = get_app()

WS_PATH = "/tts-ws/v1"
"""推理节点拨号接入路径"""

V2_LANGUAGES: Final[list[str]] = [
    "auto",
    "auto_yue",
    "en",
    "zh",
    "ja",
    "yue",
    "ko",
    "all_zh",
    "all_ja",
    "all_yue",
    "all_ko",
]
"""推理节点（GPT-SoVITS v2）支持的语言id；摘自节点实现，暂未从hello.capabilities读取"""

HELLO_TIMEOUT_S = 5.0
"""节点注册握手（node.hello → gateway.welcome）超时"""

HEARTBEAT_INTERVAL_S = 20
"""心跳间隔（秒），随welcome下发；实际探测由uvicorn WS Ping承担"""

HEARTBEAT_TIMEOUT_S = 60
"""心跳超时（秒），随welcome下发"""


class TTSError(Exception):
    """合成失败，code对齐协议§8错误码"""

    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_frame(mtype: str, payload: dict[str, Any] | None = None, rid: str | None = None) -> str:
    """构造协议§3.1文本帧"""
    frame: dict[str, Any] = {"type": mtype, "ts": _now_ms()}
    if rid is not None:
        frame["id"] = rid
    frame["payload"] = payload or {}
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _PendingRequest:
    """一条在途合成请求的网关侧上下文"""

    future: asyncio.Future[bytes]
    """音频结果future，收到终态时set_result/set_exception"""

    chunks: list[bytes] = field(default_factory=list)
    """已收到的音频分片（仅final轨）"""

    in_final_track: bool = False
    """当前打开音轨的role是否为final，非final轨的二进制帧不收集"""


@dataclass
class _NodeConnection:
    """一个已注册推理节点的连接上下文"""

    ws: WebSocket
    node_id: str
    session_id: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """同连接发送互斥锁：ASGI不保证多任务并发send的原子性"""

    pending: dict[str, _PendingRequest] = field(default_factory=dict)


_nodes: dict[str, _NodeConnection] = {}
"""已注册推理节点，键为node_id；同id重拨时新连接替换旧连接"""

_register_lock = asyncio.Lock()
"""节点注册表互斥锁"""


async def _send(conn: _NodeConnection, text: str) -> None:
    async with conn.send_lock:
        await conn.ws.send_text(text)


async def _send_app_error(
    conn: _NodeConnection,
    rid: str | None,
    code: int,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """向节点回应用层错误；连接将断开时发送失败仅忽略"""
    try:
        await _send(
            conn,
            _json_frame("gateway.error", {"code": code, "message": message, **(extra or {})}, rid),
        )
    except Exception:  # noqa: S110
        pass


def _pending(conn: _NodeConnection, rid: Any) -> _PendingRequest | None:
    """按id取在途请求；无id或已终态释放时返回None"""
    return conn.pending.get(rid) if isinstance(rid, str) else None


async def _handle_text(conn: _NodeConnection, raw: str) -> None:
    """路由节点发来的文本帧（协议§4消息类型总表）"""
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        await _send_app_error(conn, None, 1002, "bad_frame: JSON解析失败")
        return
    if not isinstance(frame, dict):
        await _send_app_error(conn, None, 1002, "bad_frame: 帧必须是JSON对象")
        return

    mtype = frame.get("type")
    rid = frame.get("id")
    payload = frame.get("payload") or {}

    match mtype:
        case "tts.accept" | "tts.progress":
            pass  # 受理/进度通知：网关无需动作
        case "tts.audio.begin":
            if (req := _pending(conn, rid)) is not None:
                # 本网关固定请求非流式输出，正常仅收到final轨；
                # 若节点仍发stream轨，其音频不入结果
                req.in_final_track = payload.get("role") == "final"
        case "tts.audio.end":
            if (req := _pending(conn, rid)) is not None:
                req.in_final_track = False
        case "tts.done":
            req = conn.pending.pop(rid, None) if isinstance(rid, str) else None
            if req is not None and not req.future.done():
                req.future.set_result(b"".join(req.chunks))
        case "tts.reject" | "tts.error":
            code = payload.get("code") or 2004
            message = payload.get("message") or str(mtype)
            req = conn.pending.pop(rid, None) if isinstance(rid, str) else None
            if req is not None and not req.future.done():
                req.future.set_exception(TTSError(int(code), str(message)))
            else:
                # 请求已超时/取消后的迟到错误，调用方早已放弃
                logger.debug(f"TTS节点 {conn.node_id} 迟到错误: {mtype} {code} {message}")
        case "tts.cancel.ack":
            req = conn.pending.pop(rid, None) if isinstance(rid, str) else None
            if req is not None and not req.future.done():
                req.future.set_exception(TTSError(3009, "请求已取消"))
        case "node.status":
            logger.debug(f"TTS节点 {conn.node_id} 状态: {payload}")
        case "node.bye":
            logger.info(f"TTS节点 {conn.node_id} 预告下线: {payload.get('reason', '')}")
        case _:
            # 未知类型：回应用层错误且不断开（协议§4向前兼容）
            await _send_app_error(
                conn, rid, 1003, f"unknown_type: {mtype!r}", {"origin_type": str(mtype)}
            )


def _handle_binary(conn: _NodeConnection, data: bytes) -> None:
    """解析二进制音频帧：1字节id长度 + request_id + 音频载荷（协议§3.2）"""
    if len(data) < 2:
        return
    id_len = data[0]
    if id_len == 0 or 1 + id_len > len(data):
        logger.debug(f"TTS节点 {conn.node_id} 二进制帧前缀越界，已丢弃（{len(data)}字节）")
        return
    rid = data[1 : 1 + id_len].decode("utf-8", "replace")
    if (req := conn.pending.get(rid)) is not None and req.in_final_track:
        req.chunks.append(data[1 + id_len :])


def _fail_pending(conn: _NodeConnection, code: int, message: str) -> None:
    """连接断开时将全部在途请求置为失败（协议§2.4断线语义）"""
    for req in conn.pending.values():
        if not req.future.done():
            req.future.set_exception(TTSError(code, message))
    conn.pending.clear()


async def _register_node(ws: WebSocket) -> _NodeConnection | None:
    """等待node.hello完成注册握手并回复welcome；失败时关闭连接返回None"""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_S)
    except TimeoutError:
        logger.warning(f"TTS节点注册超时（{HELLO_TIMEOUT_S}s内未收到node.hello）")
        await _close(ws, 4408, "hello timeout")
        return None
    except (WebSocketDisconnect, RuntimeError):
        return None

    try:
        hello = json.loads(raw)
        assert isinstance(hello, dict)
        payload = hello.get("payload") or {}
        assert isinstance(payload, dict)
    except (json.JSONDecodeError, AssertionError):
        logger.warning(f"TTS节点注册帧非法: {raw[:200]!r}")
        await _close(ws, 4402, "bad hello frame")
        return None
    if hello.get("type") != "node.hello":
        await _close(ws, 4402, "expected node.hello")
        return None

    node_id = str(payload.get("node_id") or "unknown-node")
    session_id = uuid.uuid4().hex
    conn = _NodeConnection(
        ws=ws,
        node_id=node_id,
        session_id=session_id,
        capabilities=dict(payload.get("capabilities") or {}),
    )
    async with _register_lock:
        old = _nodes.get(node_id)
        _nodes[node_id] = conn

    await _send(
        conn,
        _json_frame(
            "gateway.welcome",
            {
                "session_id": session_id,
                "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
                "heartbeat_timeout_s": HEARTBEAT_TIMEOUT_S,
                "server_time": _now_ms(),
            },
        ),
    )
    if old is not None:
        logger.warning(f"TTS节点 {node_id} 重复接入，关闭旧连接（session={old.session_id[:8]}…）")
        try:
            await old.ws.close(code=4400, reason="replaced by new connection")
        except Exception:  # noqa: S110
            pass
    logger.info(
        "TTS节点已注册: {}（session={}…，models={}）",
        node_id,
        session_id[:8],
        payload.get("models") or {},
    )
    return conn


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    """关闭节点连接；连接已断开时仅忽略"""
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: S110
        pass


@app.websocket(WS_PATH)
async def tts_node_endpoint(ws: WebSocket) -> None:
    """推理节点接入端点：鉴权 → node.hello注册 → 循环分发在途消息"""
    # 鉴权（协议§2.1）：Authorization: Bearer头或?token=查询参数；
    # accept前close即拒绝握手（HTTP 403），节点侧按退避重试
    expected = cfg.auth_token
    if expected:
        supplied = ws.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not supplied:
            supplied = ws.query_params.get("token", "")
        if supplied != expected:
            logger.warning("TTS节点接入鉴权失败，已拒绝握手")
            await ws.close()
            return

    await ws.accept()
    conn = await _register_node(ws)
    if conn is None:
        return

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if (text := msg.get("text")) is not None:
                await _handle_text(conn, text)
            elif (data := msg.get("bytes")) is not None:
                _handle_binary(conn, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(f"TTS节点 {conn.node_id} 连接异常")
    finally:
        async with _register_lock:
            if _nodes.get(conn.node_id) is conn:
                del _nodes[conn.node_id]
        logger.info(f"TTS节点已断开: {conn.node_id}（session={conn.session_id[:8]}…）")
        _fail_pending(conn, 3001, "TTS推理节点断线，在途请求作废")


def node_connected() -> bool:
    """是否已有推理节点注册"""
    return bool(_nodes)


async def _send_cancel(node: _NodeConnection, rid: str, reason: str = "gateway timeout") -> None:
    """超时兜底：请求节点取消在途推理以释放资源（协议tts.cancel）"""
    try:
        await _send(node, _json_frame("tts.cancel", {"reason": reason}, rid))
    except Exception as e:
        logger.debug(f"发送tts.cancel失败（{node.node_id}/{rid}）: {e}")


async def synthesize(
    text: str,
    text_lang: str,
    *,
    ref_audio_path: str,
    prompt_text: str,
    prompt_lang: str,
) -> bytes:
    """向已连接的推理节点发起一次合成，返回完整音频（WAV）字节

    Raises:
        TTSError: 节点未连接、被拒绝、推理失败或超时（code对齐协议§8）
    """
    node = next(iter(_nodes.values()), None)
    if node is None:
        raise TTSError(3001, "TTS推理节点未连接，无法合成语音")

    # 语言id预检：尽早失败并给出模型可读的错误
    if text_lang not in V2_LANGUAGES:
        raise TTSError(2002, f"不支持的语言id {text_lang}，支持: {V2_LANGUAGES}")
    max_chars = node.capabilities.get("max_text_chars")
    if isinstance(max_chars, int) and len(text) > max_chars:
        raise TTSError(2003, f"文本过长（{len(text)}字符，上限{max_chars}字符）")

    rid = "r-" + uuid.uuid4().hex
    req = _PendingRequest(future=asyncio.get_running_loop().create_future())
    node.pending[rid] = req
    # 固定请求非流式WAV：网关只需要一整段音频，由最终消息端自行转码
    payload = {
        "text": text,
        "text_lang": text_lang,
        "ref_audio_path": ref_audio_path,
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang,
        "media_type": "wav",
        "streaming_mode": 0,
        "text_split_method": cfg.text_split_method,
        "speed_factor": cfg.speed_factor,
        "seed": cfg.seed,
    }
    timeout = cfg.request_timeout_s
    try:
        await _send(node, _json_frame("tts.request", payload, rid))
        return await asyncio.wait_for(req.future, timeout)
    except TimeoutError:
        await _send_cancel(node, rid)
        raise TTSError(3002, f"语音合成超时（{timeout:.0f}秒）") from None
    except TTSError:
        raise
    except Exception as e:
        # 发送失败或连接层异常：统一转为节点离线语义
        raise TTSError(3001, f"发送合成请求失败: {e}") from e
    finally:
        node.pending.pop(rid, None)

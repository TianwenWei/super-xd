"""Volcengine optimized streaming ASR, with independent server-only credentials.

Protocol: https://docs.volcengine.com/docs/6561/1354869?lang=zh
Audio is live 16 kHz mono signed little-endian PCM. No TTS credentials, old
endpoint/language settings, transcription smoothing, or simulated results are
used. ``open`` confirms transport/config delivery; provider validation may
arrive only after audio because bigmodel_async responds on result changes.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import math
import os
from pathlib import Path
import struct
import time
from typing import Any, AsyncIterator
from uuid import uuid4
import zlib

from dotenv import dotenv_values

from backend.textutil import is_identifier, is_placeholder, redact_bearer, redact_header_values

ASR_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
ASR_RESOURCE_ID = "volc.seedasr.sauc.duration"
API_KEY_FIELD = "VOLCENGINE_ASR_API_KEY"
APP_ID_FIELD = "VOLCENGINE_ASR_APP_ID"
ACCESS_TOKEN_FIELD = "VOLCENGINE_ASR_ACCESS_TOKEN"
RESOURCE_FIELD = "VOLCENGINE_ASR_RESOURCE_ID"
CONFIG_FILE_FIELD = "VOLCENGINE_ASR_CONFIG_FILE"
TIMEOUT_FIELD = "VOLCENGINE_ASR_TIMEOUT_SECONDS"
SAMPLE_RATE = 16000
CHANNELS = 1
MAX_FRAME_AUDIO_BYTES = 6400  # 200 ms of 16 kHz mono PCM_s16le.
MAX_AUDIO_BYTES = 1_920_000  # 60 s; capture wall time is also bounded.
MAX_RESULT_BYTES = 1024 * 1024
MAX_RECORD_SECONDS = 60

# Deliberately excludes endpoint, language, TTS and every unrelated field.
_FILE_FIELDS = {
    API_KEY_FIELD: (API_KEY_FIELD, "VOLC_ASR_API_KEY"),
    APP_ID_FIELD: (APP_ID_FIELD, "VOLC_ASR_APP_ID"),
    ACCESS_TOKEN_FIELD: (ACCESS_TOKEN_FIELD, "VOLC_ASR_ACCESS_TOKEN"),
    RESOURCE_FIELD: (RESOURCE_FIELD, "VOLC_ASR_RESOURCE_ID"),
}


def _literal(value: str) -> bool:
    return not is_placeholder(value)


def _file_config(path: str) -> dict[str, str]:
    """Read only literal ASR fields; dotenv interpolation and shell execution off."""
    try:
        file = Path(path).expanduser()
        if file.stat().st_size > 1024 * 1024:
            raise RuntimeError("ASR 配置文件超过 1 MiB，未读取凭证。")
        with file.open(encoding="utf-8") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise RuntimeError(f"ASR 配置文件不可读取，请检查 {CONFIG_FILE_FIELD} 的路径与权限。") from None
    selected = {}
    for field, aliases in _FILE_FIELDS.items():
        for name in aliases:
            value = values.get(name)
            if value is not None and value.strip():
                selected[field] = value.strip()
                break
    return selected


def _encode(message_type: int, payload: bytes, *, serialization: int,
            sequence: int, final: bool = False) -> bytes:
    if sequence == 0 or not -(2**31) <= sequence < 2**31:
        raise ValueError("ASR sequence 必须是非零有符号 32 位整数。")
    flags = 3 if final else 1
    wire_sequence = -abs(sequence) if final else abs(sequence)
    compressed = gzip.compress(payload, mtime=0)
    return (bytes([0x11, (message_type << 4) | flags, (serialization << 4) | 1, 0])
            + struct.pack(">iI", wire_sequence, len(compressed)) + compressed)


def encode_full_request(request_id: str, sequence: int = 1) -> bytes:
    payload = {
        "user": {"uid": request_id},
        "audio": {"format": "pcm", "codec": "raw", "rate": SAMPLE_RATE,
                  "bits": 16, "channel": CHANNELS},
        "request": {"model_name": "bigmodel", "result_type": "full"},
    }
    return _encode(1, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   serialization=1, sequence=sequence)


def encode_audio(pcm: bytes, sequence: int, *, final: bool = False) -> bytes:
    return _encode(2, pcm, serialization=0, sequence=sequence, final=final)


def _inflate(payload: bytes, limit: int) -> bytes:
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        expanded = inflater.decompress(payload, limit + 1)
        if len(expanded) > limit or inflater.unconsumed_tail:
            raise RuntimeError("ASR 解压后响应超过大小上限。")
        if not inflater.eof or inflater.unused_data:
            raise RuntimeError("ASR gzip 响应不完整或含额外数据。")
        return expanded
    except zlib.error:
        raise RuntimeError("ASR gzip 响应无效。") from None


def decode_response(raw: bytes, *, max_payload_bytes: int = MAX_RESULT_BYTES) -> dict[str, Any]:
    """Decode one bounded provider frame; whole-request final comes from flags.

    The official final-response example has flags=3 with a positive sequence,
    despite its narrative specifying a negative sequence. We preserve the signed
    value for diagnostics and use the documented last-packet flag as authority.
    An utterance's ``definite`` is never a request-level terminal marker.
    """
    if not isinstance(raw, bytes) or len(raw) < 8:
        raise RuntimeError("ASR 二进制响应不完整。")
    version, header_size = raw[0] >> 4, (raw[0] & 15) * 4
    message_type, flags = raw[1] >> 4, raw[1] & 15
    serialization, compression = raw[2] >> 4, raw[2] & 15
    if version != 1 or header_size < 4 or header_size > len(raw) - 4:
        raise RuntimeError("ASR 响应协议版本或头部长度无效。")
    if message_type not in (9, 15) or flags not in (0, 1, 2, 3):
        raise RuntimeError("ASR 响应类型或序列标记无效。")
    if serialization not in (0, 1) or compression not in (0, 1):
        raise RuntimeError("ASR 响应序列化或压缩格式不支持。")
    offset, sequence, error_code = header_size, None, None
    if message_type == 15:
        if flags != 0 or offset + 8 > len(raw):
            raise RuntimeError("ASR 错误响应头部无效。")
        error_code = struct.unpack_from(">I", raw, offset)[0]
        offset += 4
    elif flags & 1:
        if offset + 8 > len(raw):
            raise RuntimeError("ASR 序列响应不完整。")
        sequence = struct.unpack_from(">i", raw, offset)[0]
        offset += 4
        if sequence == 0 or (flags == 1 and sequence < 0):
            raise RuntimeError("ASR 响应序列无效。")
    if offset + 4 > len(raw):
        raise RuntimeError("ASR 响应缺少 payload 长度。")
    size = struct.unpack_from(">I", raw, offset)[0]
    offset += 4
    if size > max_payload_bytes or len(raw) > max_payload_bytes + 80:
        raise RuntimeError("ASR 响应超过大小上限。")
    if size != len(raw) - offset:
        raise RuntimeError("ASR 响应 payload 长度不匹配。")
    payload = raw[offset:]
    if compression:
        payload = _inflate(payload, max_payload_bytes)
    try:
        text = payload.decode("utf-8")
        if serialization == 1:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("object required")
        elif message_type == 15:
            data = {"message": text}
        else:
            raise ValueError("server response must be JSON")
    except (UnicodeError, ValueError):
        raise RuntimeError("ASR 响应不是有效的 UTF-8 JSON 对象。") from None
    return {"message_type": message_type, "flags": flags, "sequence": sequence,
            "is_last": bool(flags & 2), "error_code": error_code, "data": data}


async def _close_ws(ws) -> None:
    try:
        await asyncio.wait_for(ws.close(), timeout=1)
    except (Exception, asyncio.CancelledError):
        pass


class VolcanoASR:
    def __init__(self, *, api_key: str | None = None, app_id: str | None = None,
                 access_token: str | None = None, resource_id: str | None = None,
                 timeout_seconds: float | None = None, ws_connect=None,
                 max_audio_bytes: int = MAX_AUDIO_BYTES,
                 max_result_bytes: int = MAX_RESULT_BYTES,
                 max_record_seconds: float = MAX_RECORD_SECONDS):
        explicit = {
            API_KEY_FIELD: os.getenv(API_KEY_FIELD, "") if api_key is None else api_key,
            APP_ID_FIELD: os.getenv(APP_ID_FIELD, "") if app_id is None else app_id,
            ACCESS_TOKEN_FIELD: os.getenv(ACCESS_TOKEN_FIELD, "") if access_token is None else access_token,
            RESOURCE_FIELD: os.getenv(RESOURCE_FIELD, "") if resource_id is None else resource_id,
        }
        explicit = {name: value.strip() for name, value in explicit.items()}
        self._source = "environment"
        self._configuration_error = None
        source = {}
        path = os.getenv(CONFIG_FILE_FIELD, "").strip()
        if path:
            try:
                source = _file_config(path)
            except RuntimeError as exc:
                # Complete explicit credentials don't depend on an optional file.
                if not (explicit[API_KEY_FIELD] or
                        (explicit[APP_ID_FIELD] and explicit[ACCESS_TOKEN_FIELD])):
                    self._configuration_error = str(exc)
        values = {name: explicit[name] or source.get(name, "") for name in explicit}
        if not explicit[API_KEY_FIELD] and explicit[APP_ID_FIELD] and explicit[ACCESS_TOKEN_FIELD]:
            # A complete legacy environment credential pair also outranks a
            # file API key. Never mix two independently configured auth modes.
            values[API_KEY_FIELD] = ""
        # Explicit empty constructor values support deterministic dependency
        # injection and cannot unexpectedly inherit ambient file credentials.
        for name, value in ((API_KEY_FIELD, api_key), (APP_ID_FIELD, app_id),
                            (ACCESS_TOKEN_FIELD, access_token), (RESOURCE_FIELD, resource_id)):
            if value is not None:
                values[name] = value.strip()
        self._api_key = values[API_KEY_FIELD]
        self._app_id = values[APP_ID_FIELD]
        self._access_token = values[ACCESS_TOKEN_FIELD]
        self.resource_id = values[RESOURCE_FIELD] or ("" if resource_id == "" else ASR_RESOURCE_ID)
        if (not explicit[API_KEY_FIELD] and self._api_key) or (
                not (explicit[APP_ID_FIELD] and explicit[ACCESS_TOKEN_FIELD]) and
                self._app_id and self._access_token):
            self._source = "config_file"
        self._secrets = set(filter(None, (self._api_key, self._app_id, self._access_token,
                                         source.get(API_KEY_FIELD), source.get(APP_ID_FIELD),
                                         source.get(ACCESS_TOKEN_FIELD))))
        active_auth = ((API_KEY_FIELD, self._api_key),) if self._api_key else (
            (APP_ID_FIELD, self._app_id), (ACCESS_TOKEN_FIELD, self._access_token))
        for name, value in (*active_auth, (RESOURCE_FIELD, self.resource_id)):
            if value and (not _literal(value) or "\r" in value or "\n" in value):
                self._configuration_error = f"ASR 配置 {name} 是占位符、动态引用或无效字面量。"
        try:
            self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else
                                         os.getenv(TIMEOUT_FIELD, "15"))
            if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
                raise ValueError
        except (TypeError, ValueError):
            self.timeout_seconds = 15
            self._configuration_error = f"ASR 配置 {TIMEOUT_FIELD} 必须是正数秒。"
        self.max_audio_bytes = min(max_audio_bytes, MAX_AUDIO_BYTES)
        self.max_result_bytes = min(max_result_bytes, MAX_RESULT_BYTES)
        self.max_record_seconds = min(max_record_seconds, MAX_RECORD_SECONDS)
        if min(self.max_audio_bytes, self.max_result_bytes, self.max_record_seconds) <= 0:
            raise ValueError("ASR 大小和录音时长上限必须大于零。")
        self._ws_connect = ws_connect
        self._connections: set[ASRConnection] = set()
        self._closing = False

    def info(self) -> dict[str, Any]:
        missing = []
        if not self._api_key and not (self._app_id and self._access_token):
            if self._app_id:
                missing.append(ACCESS_TOKEN_FIELD)
            elif self._access_token:
                missing.append(APP_ID_FIELD)
            else:
                missing.append(API_KEY_FIELD)
        if not self.resource_id:
            missing.append(RESOURCE_FIELD)
        result = {"configured": not missing and not self._configuration_error,
                  "missing_fields": missing, "provider": "volcengine-speech-asr",
                  "sample_rate": SAMPLE_RATE, "channels": CHANNELS,
                  "format": "pcm_s16le", "resource_id": self.resource_id,
                  "source": self._source}
        if self._configuration_error:
            result["error"] = self._configuration_error
        return result

    def safe_message(self, value: Any) -> str:
        """Public diagnostics, never request headers or credential values."""
        message = str(value)
        secrets = self._secrets | set(filter(None, (
            os.getenv("ARK_API_KEY"), os.getenv("TAVILY_API_KEY"),
            os.getenv("VOLCENGINE_SPEECH_API_KEY"), os.getenv("VOLCENGINE_SPEECH_APP_ID"),
            os.getenv("VOLCENGINE_SPEECH_ACCESS_TOKEN"))))
        for secret in sorted(secrets, key=len, reverse=True):
            message = message.replace(secret, "[redacted]")
        message = redact_header_values(redact_bearer(message),
                                       ("x-api-key", "x-api-access-key", "x-api-app-key", "authorization"))
        return message[:320]

    def _headers(self, request_id: str) -> dict[str, str]:
        info = self.info()
        if self._configuration_error:
            raise RuntimeError(self._configuration_error)
        if not info["configured"]:
            raise RuntimeError("火山 ASR 未配置，缺少 " + "、".join(info["missing_fields"]) +
                               "。需要独立 ASR 凭证及已开通的资源；TTS 凭证不能替代。")
        headers = {"X-Api-Resource-Id": self.resource_id, "X-Api-Request-Id": request_id,
                   "X-Api-Connect-Id": str(uuid4()), "X-Api-Sequence": "-1"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            # ASR's legacy header is App-Key; TTS's App-Id is a different API.
            headers["X-Api-App-Key"] = self._app_id
            headers["X-Api-Access-Key"] = self._access_token
        return headers

    async def open(self, request_id: str) -> ASRConnection:
        if self._closing:
            raise RuntimeError("ASR 适配器已关闭。")
        if not isinstance(request_id, str) or not is_identifier(request_id, max_length=128, extra="_.:-", alnum_first=True):
            raise ValueError("ASR request_id 必须是 1 至 128 位安全标识符。")
        headers = self._headers(request_id)
        connect = self._ws_connect
        if connect is None:
            from websockets.asyncio.client import connect
        started, ws, connection = time.perf_counter(), None, None
        try:
            ws = await asyncio.wait_for(connect(
                ASR_URL, additional_headers=headers, open_timeout=min(10, self.timeout_seconds),
                close_timeout=1, max_size=self.max_result_bytes + 80, max_queue=4,
                compression=None), self.timeout_seconds)
            if self._closing:
                raise RuntimeError("ASR 适配器已关闭。")
            connection = ASRConnection(self, ws, request_id, started)
            self._connections.add(connection)
            connection.metrics["connect_ms"] = connection._elapsed()
            await connection._initialize()
            return connection
        except BaseException as exc:
            if connection is not None:
                await connection.aclose()
            elif ws is not None:
                await _close_ws(ws)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, (RuntimeError, ValueError)):
                raise RuntimeError(self.safe_message(exc)) from None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            diagnostic = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
            raise RuntimeError("火山 ASR 连接或初始化失败：" + diagnostic) from None

    async def aclose(self) -> None:
        self._closing = True
        await asyncio.gather(*(connection.aclose() for connection in tuple(self._connections)))


class ASRConnection:
    def __init__(self, adapter: VolcanoASR, ws, request_id: str, started: float):
        self.adapter, self.ws, self.request_id = adapter, ws, request_id
        self._started = started
        self._record_deadline = started + adapter.max_record_seconds
        self._final_deadline: float | None = None
        self._sequence = 1
        self._send_lock = asyncio.Lock()
        self._finished = asyncio.Event()
        self._closed = False
        self._reading = False
        self._pending: set[asyncio.Task] = set()
        self.metrics: dict[str, Any] = {"request_id": request_id, "audio_bytes": 0,
                                        "audio_ms": 0, "first_response_ms": None,
                                        "first_partial_ms": None, "final_ms": None}
        headers = getattr(getattr(ws, "response", None), "headers", {})
        # Only allowlisted diagnostic response headers, never authentication.
        self.metrics["log_id"] = headers.get("X-Tt-Logid") if headers else None

    def _elapsed(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 3)

    async def _initialize(self) -> None:
        await asyncio.wait_for(self.ws.send(encode_full_request(self.request_id, self._sequence)),
                               self.adapter.timeout_seconds)
        self._sequence += 1
        self.metrics["config_sent_ms"] = self._elapsed()
        # Transport setup does not consume the user's recording allowance.
        self._record_deadline = time.perf_counter() + self.adapter.max_record_seconds

    async def send_audio(self, pcm: bytes) -> None:
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError("ASR 音频必须是非空、完整 16 位采样的 PCM bytes。")
        if len(pcm) > MAX_FRAME_AUDIO_BYTES:
            raise ValueError("ASR 每帧音频不能超过 6400 bytes（200 毫秒）。")
        async with self._send_lock:
            if self._closed or self._finished.is_set():
                raise RuntimeError("ASR 输入已关闭或结束。")
            if time.perf_counter() >= self._record_deadline:
                raise RuntimeError("ASR 录音超过 60 秒上限。")
            if self.metrics["audio_bytes"] + len(pcm) > self.adapter.max_audio_bytes:
                raise ValueError("ASR 音频超过 60 秒或配置大小上限。")
            try:
                await asyncio.wait_for(self.ws.send(encode_audio(pcm, self._sequence)),
                                       self.adapter.timeout_seconds)
            except asyncio.CancelledError:
                await self.aclose()
                raise
            except Exception as exc:
                await self.aclose()
                raise RuntimeError("ASR 音频发送失败：" + type(exc).__name__) from None
            self._sequence += 1
            self.metrics["audio_bytes"] += len(pcm)
            self.metrics["audio_ms"] = self.metrics["audio_bytes"] / (SAMPLE_RATE * 2) * 1000

    async def finish(self) -> None:
        """Send the official last audio packet: negative sequence + empty PCM."""
        async with self._send_lock:
            if self._finished.is_set():
                return
            if self._closed:
                raise RuntimeError("ASR 连接已关闭。")
            self._final_deadline = time.perf_counter() + self.adapter.timeout_seconds
            self._finished.set()
            try:
                await asyncio.wait_for(self.ws.send(encode_audio(b"", self._sequence, final=True)),
                                       self.adapter.timeout_seconds)
            except asyncio.CancelledError:
                await self.aclose()
                raise
            except Exception as exc:
                await self.aclose()
                raise RuntimeError("ASR 停止发送失败：" + type(exc).__name__) from None
            self._sequence += 1
            self.metrics["stop_sent_ms"] = self._elapsed()

    async def _receive(self):
        recv = asyncio.create_task(self.ws.recv())
        self._pending.add(recv)
        stop = None
        try:
            if not self._finished.is_set():
                stop = asyncio.create_task(self._finished.wait())
                self._pending.add(stop)
                remaining = self._record_deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait((recv, stop), timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    return recv.result()
                if stop not in done:
                    raise TimeoutError
            remaining = (self._final_deadline or time.perf_counter()) - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError
            return await asyncio.wait_for(recv, remaining)
        finally:
            tasks = [recv] + ([stop] if stop else [])
            for task in tasks:
                self._pending.discard(task)
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def results(self) -> AsyncIterator[dict[str, Any]]:
        if self._reading:
            raise RuntimeError("ASR results 只能有一个消费者。")
        if self._closed:
            raise RuntimeError("ASR 连接已关闭。")
        self._reading = True
        last_text = ""
        terminal = False
        try:
            while not self._closed:
                raw = await self._receive()
                frame = decode_response(raw, max_payload_bytes=self.adapter.max_result_bytes)
                self.metrics["server_sequence"] = frame["sequence"]
                if self.metrics["first_response_ms"] is None:
                    self.metrics["first_response_ms"] = self._elapsed()
                data = frame["data"]
                code = frame["error_code"] if frame["message_type"] == 15 else data.get("code")
                if frame["message_type"] == 15 or code not in (None, 0, 20000000):
                    detail = self.adapter.safe_message(data.get("message", data.get("msg", "")))
                    raise RuntimeError(f"火山 ASR 返回错误 {code}" + (f"：{detail}" if detail else "。"))
                result = data.get("result")
                if isinstance(result, list):
                    result = result[0] if result else None
                if result is not None and not isinstance(result, dict):
                    raise RuntimeError("ASR 响应的 result 字段类型无效。")
                text = result.get("text") if isinstance(result, dict) else None
                if text is not None and not isinstance(text, str):
                    raise RuntimeError("ASR 响应的 text 字段类型无效。")
                if frame["is_last"]:
                    if not self._finished.is_set():
                        raise RuntimeError("ASR 在停止输入前提前终结，未提交转写。")
                    terminal = True
                    self.metrics["final_ms"] = self._elapsed()
                    yield {"type": "final", "text": last_text if text is None else text,
                           "metrics": dict(self.metrics)}
                    return
                if text is not None and text != last_text:
                    last_text = text
                    if self.metrics["first_partial_ms"] is None:
                        self.metrics["first_partial_ms"] = self._elapsed()
                    yield {"type": "partial", "text": text, "metrics": dict(self.metrics)}
            if not terminal:
                raise RuntimeError("ASR 连接已关闭，未收到完整请求终结标记。")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            phase = "停止后等待最终转写" if self._finished.is_set() else "录音"
            raise RuntimeError(f"ASR {phase}超时，未收到完整请求终结标记。") from None
        except RuntimeError as exc:
            raise RuntimeError(self.adapter.safe_message(exc)) from None
        except Exception as exc:
            raise RuntimeError("ASR 连接中断，未完成转写：" + type(exc).__name__) from None
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(task for task in self._pending if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_ws(self.ws)
        self.adapter._connections.discard(self)

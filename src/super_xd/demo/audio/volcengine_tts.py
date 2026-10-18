"""Volcengine Speech TTS 2.0 adapter, with one fixed Xiaohe voice.

V3 bidirectional WebSocket yields live PCM while text is still arriving. The
older HTTP helper remains available for complete MP3 synthesis. Credentials
never enter the browser; unavailable Speech has no synthetic audio fallback.
"""
from __future__ import annotations

import base64
import binascii
import asyncio
import json
import os
from pathlib import Path
import time
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx

from super_xd.nlp.textutil import is_placeholder

TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
TTS_RESOURCE_ID = "seed-tts-2.0"
XIAOHE_VOICE = "zh_female_xiaohe_uranus_bigtts"
API_KEY_FIELD = "VOLCENGINE_SPEECH_API_KEY"
APP_ID_FIELD = "VOLCENGINE_SPEECH_APP_ID"
ACCESS_TOKEN_FIELD = "VOLCENGINE_SPEECH_ACCESS_TOKEN"
CONFIG_FILE_FIELD = "VOLCENGINE_SPEECH_CONFIG_FILE"
MAX_TEXT_CHARS = 4000
MAX_AUDIO_BYTES = 20 * 1024 * 1024


def _read_speech_config(path: str) -> str:
    """Read two literal xcconfig fields only; no includes, substitutions or code."""
    try:
        file = Path(path).expanduser()
        if file.stat().st_size > 1024 * 1024:
            raise RuntimeError("Speech xcconfig 文件过大，未读取凭证。")
        content = file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RuntimeError(f"Speech 凭证文件不可读取，请检查 {CONFIG_FILE_FIELD} 的路径与权限。") from None
    # xcconfig comments are ignored, while values are never evaluated. Other
    # fields and #include directives do not participate in this adapter.
    while (start := content.find("/*")) >= 0:
        end = content.find("*/", start + 2)
        content = content[:start] + ("" if end < 0 else content[end + 2:])
    fields: dict[str, str] = {}
    for line in content.splitlines():
        # Strip // comments outside literal quotes without decoding escapes.
        quote = None
        for index, character in enumerate(line):
            if character in ("'", '"'):
                if quote is None:
                    quote = character
                elif quote == character:
                    quote = None
            if quote is None and line[index:index + 2] == "//":
                line = line[:index]
                break
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in ("TTS_API_KEY", "TTS_RESOURCE_ID"):
            continue
        if name in fields:
            raise RuntimeError(f"Speech xcconfig 中 {name} 重复，未使用不明确的凭证。")
        value = value.strip().removesuffix(";").strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise RuntimeError(f"Speech xcconfig 中 {name} 的字面量格式无效。")
            value = value[1:-1].strip()
        if not value:
            raise RuntimeError(f"Speech xcconfig 缺少有效 {name}。")
        if is_placeholder(value):
            raise RuntimeError(f"Speech xcconfig 中 {name} 是占位符或动态引用，需要真实静态值。")
        fields[name] = value
    for name in ("TTS_API_KEY", "TTS_RESOURCE_ID"):
        if name not in fields:
            raise RuntimeError(f"Speech xcconfig 缺少 {name} 字段，未读取其他凭证。")
    if fields["TTS_RESOURCE_ID"] != TTS_RESOURCE_ID:
        raise RuntimeError(f"Speech xcconfig 的 TTS_RESOURCE_ID 不匹配，必须是 {TTS_RESOURCE_ID}。")
    return fields["TTS_API_KEY"]


class VolcanoTTS:
    def __init__(self, client: httpx.AsyncClient | None = None, *,
                 api_key: str | None = None, app_id: str | None = None,
                 access_token: str | None = None, timeout_seconds: float | None = None,
                 ws_connect=None, allowed_voices=None):
        self._api_key = (os.getenv(API_KEY_FIELD, "") if api_key is None else api_key).strip()
        self._app_id = (os.getenv(APP_ID_FIELD, "") if app_id is None else app_id).strip()
        self._access_token = (os.getenv(ACCESS_TOKEN_FIELD, "") if access_token is None else access_token).strip()
        self._allowed_voices = set(allowed_voices or (XIAOHE_VOICE,))
        if any(not isinstance(voice, str) or not voice.strip() or len(voice) > 120 for voice in self._allowed_voices):
            raise ValueError("TTS provider voice identifiers must be nonblank strings of at most 120 characters")
        self._source = "environment"
        self._configuration_error = None
        config_file = os.getenv(CONFIG_FILE_FIELD, "").strip()
        if not self._api_key and not (self._app_id and self._access_token) and config_file:
            self._source = "config_file"
            try:
                self._api_key = _read_speech_config(config_file)
            except RuntimeError as exc:
                self._configuration_error = str(exc)
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else float(
            os.getenv("VOLCENGINE_SPEECH_TTS_TIMEOUT_SECONDS", "45"))
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(10, self.timeout_seconds)),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        self._ws_connect = ws_connect
        self._speech_streams = set()
        self._speech_pool = []
        self._speech_prewarm = None
        self._closing = False

    def info(self) -> dict[str, Any]:
        """Safe to expose in /api/config: neither secret values nor lengths."""
        configured = bool(self._api_key or (self._app_id and self._access_token))
        missing = []
        if not configured and not self._configuration_error:
            if self._app_id:
                missing = [ACCESS_TOKEN_FIELD]
            elif self._access_token:
                missing = [APP_ID_FIELD]
            else:
                missing = [API_KEY_FIELD]
        info = {"configured": configured, "voice": XIAOHE_VOICE,
                "provider": "volcengine-speech-tts", "missing_fields": missing,
                "source": self._source,
                "stream_pool_ready": sum(self._connection_open(item) for item in self._speech_pool),
                "stream_prewarming": bool(self._speech_prewarm and not self._speech_prewarm.done())}
        if self._configuration_error:
            info["error"] = self._configuration_error
        return info

    def _headers(self) -> dict[str, str]:
        info = self.info()
        if self._configuration_error:
            raise RuntimeError(self._configuration_error)
        if not info["configured"]:
            raise RuntimeError(
                "火山 Speech TTS 未配置，缺少 " + "、".join(info["missing_fields"]) +
                f"。请配置独立 Speech API Key（{API_KEY_FIELD}），或旧版 {APP_ID_FIELD} 与 "
                f"{ACCESS_TOKEN_FIELD}；ARK_API_KEY 不能替代。")
        headers = {"Content-Type": "application/json", "X-Api-Resource-Id": TTS_RESOURCE_ID,
                   "X-Api-Request-Id": str(uuid4())}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            headers["X-Api-App-Id"] = self._app_id
            headers["X-Api-Access-Key"] = self._access_token
        return headers

    def _safe_message(self, value: Any) -> str:
        message = str(value)
        for secret in (self._api_key, self._app_id, self._access_token,
                       os.getenv("ARK_API_KEY", ""), os.getenv("TAVILY_API_KEY", "")):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message[:240]

    def _voice(self, voice):
        selected = XIAOHE_VOICE if voice in (None, "", "xiaohe2.0", "小何2.0") else voice
        if selected not in self._allowed_voices:
            raise RuntimeError("本原型仅支持火山 Speech 小何 2.0 音色，不能替换为其他音色。")
        return selected

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        return (await self.synthesize_with_metrics(text, voice))["audio"]

    async def stream_synthesize(self, texts: AsyncIterator[str], on_event=None,
                                voice: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield live Xiaohe PCM frames while upstream text is still arriving."""
        adapter_started = time.perf_counter()
        selected_voice = self._voice(voice)
        from super_xd.demo.audio.speech_stream import SpeechStream
        headers = self._stream_headers()
        prewarm_wait_ms = 0
        if self._speech_prewarm and not self._speech_prewarm.done():
            prewarm_started = time.perf_counter()
            await asyncio.shield(self._speech_prewarm)
            prewarm_wait_ms = (time.perf_counter() - prewarm_started) * 1000
        connection = None
        while self._speech_pool:
            candidate = self._speech_pool.pop()
            if self._connection_open(candidate):
                connection = candidate
                break
            await candidate['websocket'].close()
        stream = SpeechStream(headers, selected_voice, connect=self._ws_connect,
            timeout_seconds=self.timeout_seconds, max_text_chars=MAX_TEXT_CHARS,
            max_audio_bytes=MAX_AUDIO_BYTES, safe_message=self._safe_message,
            connection=connection, recycle=self._recycle_speech_connection,
            adapter_started=adapter_started, lease_wait_ms=(time.perf_counter() - adapter_started) * 1000,
            prewarm_wait_ms=prewarm_wait_ms)
        self._speech_streams.add(stream)
        events = stream.events(texts, on_event)
        try:
            async for item in events:
                yield item
        finally:
            await events.aclose()
            await stream.aclose()
            self._speech_streams.discard(stream)

    def _stream_headers(self):
        if self._closing:
            raise RuntimeError('火山 Speech TTS 客户端已关闭。')
        headers = self._headers()
        headers.pop('Content-Type', None)
        headers.pop('X-Api-Request-Id', None)
        headers['X-Api-Connect-Id'] = str(uuid4())
        headers['X-Control-Require-Usage-Tokens-Return'] = '*'
        return headers

    @staticmethod
    def _connection_open(connection):
        websocket = connection['websocket']
        state = getattr(websocket, 'state', None)
        return not getattr(websocket, 'closed', False) and (state is None or state == 1)

    async def _recycle_speech_connection(self, connection):
        if self._closing or not self._connection_open(connection) or len(self._speech_pool) >= 2:
            return False
        self._speech_pool.append(connection)
        return True

    async def prewarm_stream(self) -> dict[str, Any]:
        """Prepare a clean authenticated connection without synthesizing text."""
        from super_xd.demo.audio.speech_stream import SpeechStream
        headers = self._stream_headers()
        for connection in tuple(self._speech_pool):
            if not self._connection_open(connection):
                self._speech_pool.remove(connection)
                await connection['websocket'].close()
        if any(self._connection_open(item) for item in self._speech_pool):
            return {'ok': True, 'reused': True, 'pool_ready': len(self._speech_pool)}

        async def prepare():
            stream = SpeechStream(headers, XIAOHE_VOICE, connect=self._ws_connect,
                                  timeout_seconds=self.timeout_seconds, safe_message=self._safe_message)
            try:
                connection = await stream.open_connection()
                if not await self._recycle_speech_connection(connection):
                    await stream.aclose()
                return {'ok': True, 'reused': False, 'pool_ready': len(self._speech_pool),
                        'connect_ms': connection['connect_ms'],
                        'connection_ready_ms': connection['connection_ready_ms']}
            except asyncio.CancelledError:
                await stream.aclose()
                raise
            except Exception as exc:
                await stream.aclose()
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError('火山 Speech WS 预连接失败：' + self._safe_message(type(exc).__name__)) from None

        if self._speech_prewarm is None or self._speech_prewarm.done():
            self._speech_prewarm = asyncio.create_task(prepare())
        return await asyncio.shield(self._speech_prewarm)

    async def synthesize_with_metrics(self, text: str, voice: str | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise RuntimeError(f"朗读文本需为 1–{MAX_TEXT_CHARS} 字，不能只含空白。")
        selected_voice = self._voice(voice)
        headers = self._headers()
        payload = {"req_params": {"text": text, "speaker": selected_voice,
                   "audio_params": {"format": "mp3", "sample_rate": 24000},
                   "additions": json.dumps({"disable_markdown_filter": True,
                                            "disable_emoji_filter": True})}}
        started = time.perf_counter()
        first_packet_ms = None
        audio = bytearray()
        buffer = ""
        decoder = json.JSONDecoder()
        completed = False
        frames = 0
        try:
            async with self.client.stream("POST", TTS_URL, headers=headers, json=payload,
                                          timeout=self.timeout_seconds) as response:
                if response.status_code >= 400:
                    error = RuntimeError(f"火山 Speech TTS 请求失败（HTTP {response.status_code}）。"
                                         "请检查独立 Speech 凭证、seed-tts-2.0 开通权限与额度。")
                    error.status_code = response.status_code
                    raise error
                async for chunk in response.aiter_text():
                    buffer += chunk
                    if len(buffer) > MAX_AUDIO_BYTES * 2:
                        raise RuntimeError("火山 Speech TTS 响应超过大小上限，已停止。")
                    while buffer.strip():
                        buffer = buffer.lstrip("\ufeff \r\n\t")
                        try:
                            frame, end = decoder.raw_decode(buffer)
                        except json.JSONDecodeError:
                            # HTTP chunks need not align with JSON frames/UTF-8.
                            break
                        buffer = buffer[end:]
                        frames += 1
                        if not isinstance(frame, dict) or type(frame.get("code")) is not int:
                            raise RuntimeError("火山 Speech TTS 返回了无效的响应结构。")
                        code = frame["code"]
                        if code not in (0, 20000000):
                            raise RuntimeError(f"火山 Speech TTS 合成失败（code {code}）："
                                               + self._safe_message(frame.get("message", "未知错误")))
                        encoded = frame.get("data")
                        if encoded:
                            if not isinstance(encoded, str):
                                raise RuntimeError("火山 Speech TTS 返回了无效的音频编码。")
                            try:
                                decoded = base64.b64decode(encoded, validate=True)
                            except (ValueError, binascii.Error):
                                raise RuntimeError("火山 Speech TTS 返回了无效的音频编码。") from None
                            if decoded and first_packet_ms is None:
                                first_packet_ms = (time.perf_counter() - started) * 1000
                            audio.extend(decoded)
                            if len(audio) > MAX_AUDIO_BYTES:
                                raise RuntimeError("火山 Speech TTS 音频超过大小上限，已停止。")
                        if code == 20000000:
                            completed = True
                            break
                    if completed:
                        if buffer.strip():
                            raise RuntimeError("火山 Speech TTS 完成帧后存在意外数据。")
                        break
                # The documented HTTP interface permits successful code=0 JSON
                # chunks terminated by orderly HTTP EOF. Some deployments also
                # return the explicit 20000000 completion frame.
                if buffer.strip():
                    raise RuntimeError("火山 Speech TTS 响应不完整或不是有效 JSON，未返回音频。")
        except httpx.TimeoutException:
            raise RuntimeError("火山 Speech TTS 合成超时，未返回音频。") from None
        except httpx.HTTPError:
            raise RuntimeError("火山 Speech TTS 网络连接失败，未返回音频。") from None
        if not frames or not audio or first_packet_ms is None:
            raise RuntimeError("火山 Speech TTS 未返回有效音频，未使用替代朗读。")
        # A requested MP3 must have an ID3 header or MPEG frame sync, rather than
        # an API error/PCM body accidentally served to the browser as audio/mpeg.
        if not (audio.startswith(b"ID3") or (len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0)):
            raise RuntimeError("火山 Speech TTS 返回内容不是可识别的 MP3，未返回音频。")
        return {"audio": bytes(audio), "first_packet_ms": first_packet_ms,
                "total_ms": (time.perf_counter() - started) * 1000}

    async def aclose(self) -> None:
        self._closing = True
        if self._speech_prewarm and not self._speech_prewarm.done():
            self._speech_prewarm.cancel()
            await asyncio.gather(self._speech_prewarm, return_exceptions=True)
        for stream in tuple(self._speech_streams):
            await stream.aclose()
        from super_xd.demo.audio.speech_stream import encode_request
        while self._speech_pool:
            connection = self._speech_pool.pop()
            try:
                await asyncio.wait_for(connection['websocket'].send(encode_request(2)), 0.25)
            except Exception:
                pass
            await connection['websocket'].close()
        await self.client.aclose()

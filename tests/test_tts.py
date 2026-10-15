"""Offline HTTP-protocol tests only; fake MP3 bytes do not validate real speech."""
import asyncio
import base64
import json

import httpx
import pytest

from backend.tts import (VolcanoTTS, TTS_URL, TTS_RESOURCE_ID, XIAOHE_VOICE,
                         API_KEY_FIELD, APP_ID_FIELD, ACCESS_TOKEN_FIELD, CONFIG_FILE_FIELD)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def encoded(audio):
    return base64.b64encode(audio).decode()


def frame(code=0, data=None, **fields):
    return {"code": code, "message": "OK", **({"data": data} if data is not None else {}), **fields}


def stream_bytes(*frames, separator="\n"):
    return separator.join(json.dumps(row, ensure_ascii=False) for row in frames).encode()


def client_for(raw=None, *, status=200, handler=None):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda request: httpx.Response(
        status, stream=Chunks([raw or b""]), headers={"content-type": "application/json"}))))


@pytest.fixture(autouse=True)
def clear_speech_credentials(monkeypatch):
    for name in (API_KEY_FIELD, APP_ID_FIELD, ACCESS_TOKEN_FIELD, CONFIG_FILE_FIELD):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_missing_speech_key_is_explicit_and_never_uses_ark(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-model-only-secret")
    called = False

    def handler(request):
        nonlocal called
        called = True
        raise AssertionError("Must not contact the provider without Speech credentials")

    tts = VolcanoTTS(client_for(handler=handler))
    try:
        assert tts.info() == {"configured": False, "voice": XIAOHE_VOICE,
                              "provider": "volcengine-speech-tts", "missing_fields": [API_KEY_FIELD],
                              "source": "environment", "stream_pool_ready": 0, "stream_prewarming": False}
        with pytest.raises(RuntimeError, match=API_KEY_FIELD) as captured:
            await tts.synthesize("你好")
        assert "ARK_API_KEY 不能替代" in str(captured.value)
        assert "ark-model-only-secret" not in str(captured.value)
        assert not called
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_new_key_auth_and_fixed_xiaohe_mp3_payload():
    requests = []
    data = stream_bytes(frame(data=encoded(b"ID3-part-one")), frame(data=encoded(b"-part-two")), frame(20000000))

    def handler(request):
        requests.append(request)
        assert str(request.url) == TTS_URL
        assert request.headers["X-Api-Key"] == "speech-test-key"
        assert "X-Api-App-Id" not in request.headers
        assert request.headers["X-Api-Resource-Id"] == TTS_RESOURCE_ID == "seed-tts-2.0"
        assert len(request.headers["X-Api-Request-Id"]) == 36
        params = json.loads(request.content)["req_params"]
        assert params["speaker"] == XIAOHE_VOICE
        assert params["audio_params"] == {"format": "mp3", "sample_rate": 24000}
        assert params["text"] == "你好"
        assert json.loads(params["additions"])["disable_markdown_filter"] is True
        return httpx.Response(200, stream=Chunks([data[:20], data[20:70], data[70:]]))

    tts = VolcanoTTS(client_for(handler=handler), api_key="speech-test-key")
    try:
        assert tts.info()["configured"] is True
        metrics = await tts.synthesize_with_metrics("你好")
        assert metrics["audio"] == b"ID3-part-one-part-two"
        assert 0 <= metrics["first_packet_ms"] <= metrics["total_ms"]
        assert await tts.synthesize("你好", voice=XIAOHE_VOICE) == metrics["audio"]
        assert requests[0].headers["X-Api-Request-Id"] != requests[1].headers["X-Api-Request-Id"]
        assert "speech-test-key" not in json.dumps(tts.info())
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_legacy_app_token_auth_supported_and_key_has_priority():
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, content=stream_bytes(frame(data=encoded(b"ID3-audio"))))

    legacy = VolcanoTTS(client_for(handler=handler), app_id="legacy-app", access_token="legacy-token")
    modern = VolcanoTTS(client_for(handler=handler), api_key="new-key", app_id="legacy-app", access_token="legacy-token")
    try:
        assert legacy.info()["configured"] is True
        assert modern.info()["configured"] is True
        await legacy.synthesize("你好")
        await modern.synthesize("你好")
        assert seen[0]["X-Api-App-Id"] == "legacy-app"
        assert seen[0]["X-Api-Access-Key"] == "legacy-token"
        assert "X-Api-Key" not in seen[0]
        assert seen[1]["X-Api-Key"] == "new-key"
        assert "X-Api-Access-Key" not in seen[1]
    finally:
        await legacy.aclose()
        await modern.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials,missing", [({"app_id": "app"}, ACCESS_TOKEN_FIELD),
                                                  ({"access_token": "token"}, APP_ID_FIELD)])
async def test_partial_legacy_credentials_report_exact_missing_field(credentials, missing):
    tts = VolcanoTTS(client_for(), **credentials)
    try:
        assert tts.info()["missing_fields"] == [missing]
        with pytest.raises(RuntimeError, match=missing):
            await tts.synthesize("你好")
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_xcconfig_reads_only_two_literal_fields_without_execution_or_mutation(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-exist"
    source = tmp_path / "Secrets.xcconfig"
    original = ("// Speech credentials\n"
                "TTS_API_KEY = \"offline-file-key\" // local only\n"
                "TTS_RESOURCE_ID = seed-tts-2.0\n"
                f"UNRELATED = $(touch {marker})\n"
                "#include \"not-present.xcconfig\"\n")
    source.write_text(original)
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(source))

    def handler(request):
        assert request.headers["X-Api-Key"] == "offline-file-key"
        assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
        return httpx.Response(200, content=stream_bytes(frame(data=encoded(b"ID3-file-test"))))

    tts = VolcanoTTS(client_for(handler=handler))
    try:
        assert tts.info()["configured"] is True
        assert tts.info()["source"] == "config_file"
        assert "offline-file-key" not in json.dumps(tts.info())
        assert await tts.synthesize("你好") == b"ID3-file-test"
        assert not marker.exists()
        assert source.read_text() == original
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_environment_key_has_priority_over_unused_invalid_config(tmp_path, monkeypatch):
    source = tmp_path / "WrongResource.xcconfig"
    source.write_text("TTS_API_KEY = unused-file-key\nTTS_RESOURCE_ID = seed-tts-1.0\n")
    monkeypatch.setenv(API_KEY_FIELD, "preferred-env-key")
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(source))

    def handler(request):
        assert request.headers["X-Api-Key"] == "preferred-env-key"
        assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
        return httpx.Response(200, content=stream_bytes(frame(data=encoded(b"ID3-env-test"))))

    tts = VolcanoTTS(client_for(handler=handler))
    try:
        assert tts.info()["source"] == "environment"
        assert tts.info()["configured"] is True
        assert await tts.synthesize("你好") == b"ID3-env-test"
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,match", [
    ("TTS_API_KEY = offline-key\nTTS_RESOURCE_ID = seed-tts-1.0", "TTS_RESOURCE_ID 不匹配"),
    ("TTS_API_KEY = offline-key", "缺少 TTS_RESOURCE_ID"),
    ("TTS_RESOURCE_ID = seed-tts-2.0", "缺少 TTS_API_KEY"),
    ("TTS_API_KEY = YOUR_TTS_API_KEY\nTTS_RESOURCE_ID = seed-tts-2.0", "占位符或动态引用"),
    ("TTS_API_KEY = $(SECRET_KEY)\nTTS_RESOURCE_ID = seed-tts-2.0", "占位符或动态引用"),
    ("TTS_API_KEY = <missing>\nTTS_RESOURCE_ID = seed-tts-2.0", "占位符或动态引用"),
    ("TTS_API_KEY = XXXXXXXXXX\nTTS_RESOURCE_ID = seed-tts-2.0", "占位符或动态引用"),
    ("TTS_API_KEY = \nTTS_RESOURCE_ID = seed-tts-2.0", "缺少有效 TTS_API_KEY"),
    ("TTS_API_KEY = first-key\nTTS_API_KEY = second-key\nTTS_RESOURCE_ID = seed-tts-2.0", "TTS_API_KEY 重复"),
])
async def test_invalid_config_fails_safely_without_using_other_credentials(tmp_path, monkeypatch, content, match):
    source = tmp_path / "Secrets.xcconfig"
    source.write_text(content)
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(source))
    tts = VolcanoTTS(client_for())
    try:
        assert tts.info()["configured"] is False
        assert tts.info()["source"] == "config_file"
        assert "offline-key" not in json.dumps(tts.info())
        with pytest.raises(RuntimeError, match=match):
            await tts.synthesize("你好")
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_missing_config_path_is_reported_without_file_contents(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(tmp_path / "missing.xcconfig"))
    tts = VolcanoTTS(client_for())
    try:
        assert tts.info()["configured"] is False
        assert "不可读取" in tts.info()["error"]
        with pytest.raises(RuntimeError, match=CONFIG_FILE_FIELD):
            await tts.synthesize("你好")
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", ["zh_female_xiaohe_jupiter_bigtts", "zh_female_vv_uranus_bigtts", "en-US"])
async def test_other_voice_is_rejected_without_fallback(voice):
    tts = VolcanoTTS(client_for(), api_key="offline-key")
    try:
        with pytest.raises(RuntimeError, match="仅支持.*小何 2.0"):
            await tts.synthesize("你好", voice=voice)
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", "   ", "x" * 4001])
async def test_invalid_text_rejected_before_provider_call(text):
    tts = VolcanoTTS(client_for(), api_key="offline-key")
    try:
        with pytest.raises(RuntimeError, match="朗读文本需为"):
            await tts.synthesize(text)
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\n", "", "\r\n\n"])
async def test_json_frames_can_cross_transport_and_utf8_boundaries(separator):
    raw = stream_bytes(frame(data=encoded(b"ID3-one"), message="成功"),
                       frame(data=encoded(b"-two")), separator=separator)
    transport = Chunks([raw[index:index + 3] for index in range(0, len(raw), 3)])
    tts = VolcanoTTS(client_for(handler=lambda request: httpx.Response(200, stream=transport)), api_key="offline-key")
    try:
        assert await tts.synthesize("你好") == b"ID3-one-two"
        assert transport.closed
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_http_error_does_not_expose_provider_response_or_secrets():
    tts = VolcanoTTS(client_for(b'{"message":"offline-secret-key"}', status=401), api_key="offline-secret-key")
    try:
        with pytest.raises(RuntimeError, match="HTTP 401") as captured:
            await tts.synthesize("你好")
        assert "offline-secret-key" not in str(captured.value)
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_provider_error_after_audio_discards_partial_audio_and_redacts():
    raw = stream_bytes(frame(data=encoded(b"ID3-partial")),
                       frame(45000001, message="Access denied for offline-secret-key"))
    tts = VolcanoTTS(client_for(raw), api_key="offline-secret-key")
    try:
        with pytest.raises(RuntimeError, match="45000001") as captured:
            await tts.synthesize("你好")
        assert "offline-secret-key" not in str(captured.value)
        assert "[redacted]" in str(captured.value)
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,match", [
    (b"", "未返回有效音频"),
    (stream_bytes(frame()), "未返回有效音频"),
    (stream_bytes(frame(data="%%%")), "无效的音频编码"),
    (b'{"code":0,"data":', "响应不完整"),
    (b'{"code":"0","data":"SUQz"}', "无效的响应结构"),
    (b"[]", "无效的响应结构"),
    (b"provider returned HTML", "响应不完整"),
    (stream_bytes(frame(data=encoded(b"raw pcm bytes"))), "不是可识别的 MP3"),
    (stream_bytes(frame(20000000)) + b"unexpected", "完成帧后存在意外数据"),
])
async def test_malformed_or_empty_audio_has_no_substitute(raw, match):
    tts = VolcanoTTS(client_for(raw), api_key="offline-key")
    try:
        with pytest.raises(RuntimeError, match=match):
            await tts.synthesize("你好")
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,match", [(httpx.ReadTimeout("secret timeout details"), "合成超时"),
                                            (httpx.RemoteProtocolError("secret broken stream"), "网络连接失败")])
async def test_network_failures_are_friendly_and_sanitized(exception, match):
    def handler(request):
        raise exception

    tts = VolcanoTTS(client_for(handler=handler), api_key="offline-key")
    try:
        with pytest.raises(RuntimeError, match=match) as captured:
            await tts.synthesize("你好")
        assert "secret" not in str(captured.value)
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_concurrent_requests_keep_metrics_and_audio_local():
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(Chunks):
        async def __aiter__(self):
            started.set()
            await release.wait()
            async for chunk in super().__aiter__():
                yield chunk

    def handler(request):
        text = json.loads(request.content)["req_params"]["text"]
        raw = stream_bytes(frame(data=encoded(("ID3-" + text).encode())))
        return httpx.Response(200, stream=Slow([raw]) if text == "slow" else Chunks([raw]))

    tts = VolcanoTTS(client_for(handler=handler), api_key="offline-key")
    try:
        pending = asyncio.create_task(tts.synthesize_with_metrics("slow"))
        await started.wait()
        fast = await tts.synthesize_with_metrics("fast")
        release.set()
        slow = await pending
        assert fast["audio"] == b"ID3-fast"
        assert slow["audio"] == b"ID3-slow"
        assert slow["total_ms"] >= fast["total_ms"]
        assert 0 <= fast["first_packet_ms"] <= fast["total_ms"]
        assert 0 <= slow["first_packet_ms"] <= slow["total_ms"]
    finally:
        release.set()
        await tts.aclose()


@pytest.mark.asyncio
async def test_cancelled_synthesis_closes_stream_and_returns_no_audio():
    entered = asyncio.Event()

    class Blocked(Chunks):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    stream = Blocked([])
    tts = VolcanoTTS(client_for(handler=lambda request: httpx.Response(200, stream=stream)), api_key="offline-key")
    try:
        pending = asyncio.create_task(tts.synthesize("你好"))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert stream.closed
    finally:
        await tts.aclose()

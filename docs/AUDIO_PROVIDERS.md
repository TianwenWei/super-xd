# 音频服务接口与 vLLM ASR

服务选择位于 `config/services.toml`，密钥仍只存环境变量。默认火山适配器继续使用现有凭证、接口和音频管线；选择其他配置不会自动回退火山或产生模拟回复。

## 内线契约

`backend/services/audio.py` 提供 `ASRProvider`、`ASRStream`、`TTSProvider` 和不可变 `AudioFormat`。

- ASR：16kHz、单声道、PCM s16le；每段 `open(request_id)`，依次 `send_audio(pcm)`，`finish()` 结束输入，`results()` 返回 partial/final；`aclose()` 幂等取消该段。partial 是完整当前假设，final 是完整本段结果。
- TTS：增量文本输入，24kHz、单声道、PCM s16le 输出。`alignment` 只来自提供商真实句子边界；没有边界时不虚构用户已听到的内容。原始厂商事件只作为脱敏诊断保存。
- `AudioFormat.duration_ms(byte_count)` 统一计算时长。转换音频格式需要显式转换层；适配器不得给不兼容音频重新贴格式标签。
- VAD 负责分段和播报打断，识别供应商只负责本段转写。VAD 触发时暂停音频，新的实际输入才取消旧对话轮次。
- 单段取消关闭单段请求，不关闭共享服务。应用退出才调用 provider `aclose()`；超时/鉴权/限流/服务不可用等错误有统一 code，诊断脱敏。

## FireRedASR2 / vLLM

按 [vLLM Speech-to-Text 官方接口](https://docs.vllm.ai/en/stable/serving/online_serving/speech_to_text/)实现 `POST /v1/audio/transcriptions`。FireRedASR2 的部署参考 [FireRed 官方仓库](https://github.com/FireRedTeam/FireRedASR2S)。本仓库不假定该模型支持 Realtime WebSocket。

```toml
[asr]
profile = "vllm"

[asr.profiles.vllm]
provider = "vllm"
base_url_env = "ASR_BASE_URL"
model_env = "ASR_MODEL"
key_env = "ASR_API_KEY"
require_api_key = false
language = "zh"
timeout_seconds = 30
max_record_seconds = 60
```

设置 `ASR_BASE_URL=http://你的服务地址:端口/v1`、`ASR_MODEL=服务实际暴露的模型名`。有鉴权时设置 `ASR_API_KEY` 并将 `require_api_key` 改为 `true`。也可通过 `ASR_PROFILE=vllm` 选择已声明的配置，无需改 TOML 的默认项。

该适配器在本地 VAD 结束一段后，将有界 PCM 原样封装为真实 WAV，上传 multipart 字段 `file`、`model`、`response_format=json` 和可选 `language`。只接受真实 JSON `text` 字符串作为 final。状态明确报告 `partial_results=false`、`streaming_input=false`、`utterance_only=true`；段内不会显示来自模型的 partial。连续对话仍由持续 VAD 分段实现。

当前只通过协议契约测试，尚未连接用户自己的 FireRedASR2 服务验证模型、部署版本、资源和识别质量。空 endpoint/model 或必需密钥缺失会显示未配置，并在请求时直接报错。

## TTS 音色

人设使用逻辑音色 `assistant`。`tts.profiles.<名称>.voices` 将逻辑别名映射为提供商音色 ID。旧会话的小何 ID 仍兼容；自定义音色必须由配置明确授权，并由对应火山账户实际开通。修改别名不会改变车控工具选择或参数。

## 验证

`tests/test_audio_providers.py` 覆盖真实 WAV 字节/格式与 multipart 字段、鉴权和缺配、HTTP 错误、无效文本、取消上传、资源关闭、PCM 格式、逻辑音色与真实对齐。测试音频和响应均是显式离线夹具，不代表真实识别或语音合成成功。现有 ASR/TTS/VAD/音频端点和打断测试继续运行。

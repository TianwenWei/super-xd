# 音频服务接口与地区选择

服务选择位于 `config/services.toml`，密钥仍只存环境变量。未选择地区时，仓库基础配置使用火山适配器；选择其他配置不会自动回退火山或产生模拟回复。

## 地区选择

在进程启动前设置 `SERVICE_REGION=europe`，选择 ElevenLabs Scribe ASR 与火山双向流式 TTS，同时按 `config/regions.toml` 选择四个 Google EU Gemini 3.8 Flash 模型角色。TTS 复用原有 `volcengine` profile 与小何 2.0 音色 `zh_female_xiaohe_uranus_bigtts`，使用已有火山语音凭证；ASR 继续使用 `ELEVENLABS_API_KEY` 和 ElevenLabs 标准全球路由。欧洲预设名称不代表整套服务具有欧洲数据驻留保证。`SERVICE_REGION=china` 仍选择火山 ASR/TTS 和 Ark 模型。ElevenLabs Flash TTS 与 Google Chirp 3 HD TTS 都保留为显式可选 profile；后者使用 `location="eu"` 的官方端点、可用 ADC 与 `GOOGLE_CLOUD_PROJECT`。

地区选择只在启动时生效，须重启 API 与 Worker；不按 IP 猜测、不按请求失败自动切换。选中地区时忽略 `ASR_PROFILE` / `TTS_PROFILE`，避免得到半套地区配置。单独选择 vLLM 等音频 profile 前，先取消 `SERVICE_REGION`。未设置该变量仍使用显式配置，仓库没有全局欧洲默认；本机 `.env` 的欧洲选择只是本地设置。其他服务域与角色预算不会随地区改变，详见 [服务配置](SERVICE_PROVIDERS.md#地区预设)。

## 内线契约

`backend/services/audio.py` 提供 `ASRProvider`、`ASRStream`、`TTSProvider` 和不可变 `AudioFormat`。

- ASR：16kHz、单声道、PCM s16le；每段 `open(request_id)`，依次 `send_audio(pcm)`，`finish()` 结束输入，`results()` 返回 partial/final；`aclose()` 幂等取消该段。partial 是完整当前假设，final 是完整本段结果。
- TTS：增量文本输入，24kHz、单声道、PCM s16le 输出。`alignment` 只来自提供商真实句子边界；没有边界时不虚构用户已听到的内容。原始厂商事件只作为脱敏诊断保存。
- `AudioFormat.duration_ms(byte_count)` 统一计算时长。转换音频格式需要显式转换层；适配器不得给不兼容音频重新贴格式标签。
- VAD 负责分段和播报打断，识别供应商只负责本段转写。VAD 触发时暂停音频，新的实际输入才取消旧对话轮次。
- 单段取消关闭单段请求，不关闭共享服务。应用退出才调用 provider `aclose()`；超时/鉴权/限流/服务不可用等错误有统一 code，诊断脱敏。

## 流式音频提供商

| Profile | ASR | TTS |
| --- | --- | --- |
| `volcengine` | 火山实时识别 | 火山双向流式，真实句界 alignment |
| `elevenlabs` | Scribe v2 Realtime，中文、PCM16K | Flash v2.5，中文、PCM24K；逻辑音色默认公开 George |
| `google` | 未实现 Google ASR 适配器 | Chirp 3 HD，PCM24K；逻辑音色默认 Kore |
| `vllm` | 完整段 multipart 转写 | 未实现 |

ElevenLabs ASR 每段建立独立连接，以本地 VAD 的 finish 决定本段结束。普通音频立即发送；finish 使用官方空音频 manual commit，不扣留最后一帧。长段约每 20 秒按实际音频字节预提交并等待稳定结果，避免供应商自动分段与最终提交混淆；最终文本保留所有已确认片段，partial 是稳定前缀加当前假设。错误、取消和超时不会把未确认 partial 变成 final。[官方提交协议](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies)

ElevenLabs TTS 每次 utterance 使用独立、绑定音色的官方 WebSocket，配置帧后将每个实际短句用 `flush:true` 立即发送，输入结束才发 EOS；不等固定字符数。取消关闭收发任务、文本源和连接，不重发已部分播出的语音。prewarm 明确返回 `prewarmed:false`，不发送占位内容，也不声称已保留可复用连接。返回音频须为实际 PCM，奇数字节跨包重组，缺尾样本、空结果、超限或提前终止均报错；真实字符 alignment 作为诊断保留，不据此虚构句级已播放事实。[官方流式接口](https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input)

首 PCM 包、首个完整采样、可听声音和设备实际播放时间是不同指标。适配器记录连接、首 PCM 和总耗时；比较声音起点还需分析返回 PCM 的固定窗口 RMS，并记录窗口、阈值与前导静音，不能把“首包快”当成“用户更早听见”。RMS 仅是波形的声音起点估计；浏览器音频缓冲、扬声器和用户听到的片段仍由播放回执确认。

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

设置 `ASR_BASE_URL=http://你的服务地址:端口/v1`、`ASR_MODEL=服务实际暴露的模型名`。有鉴权时设置 `ASR_API_KEY` 并将 `require_api_key` 改为 `true`。未选择地区时，也可通过 `ASR_PROFILE=vllm` 选择已声明的配置，无需改 TOML 的默认项。

该适配器在本地 VAD 结束一段后，将有界 PCM 原样封装为真实 WAV，上传 multipart 字段 `file`、`model`、`response_format=json` 和可选 `language`。只接受真实 JSON `text` 字符串作为 final。状态明确报告 `partial_results=false`、`streaming_input=false`、`utterance_only=true`；段内不会显示来自模型的 partial。连续对话仍由持续 VAD 分段实现。

当前只通过协议契约测试，尚未连接用户自己的 FireRedASR2 服务验证模型、部署版本、资源和识别质量。空 endpoint/model 或必需密钥缺失会显示未配置，并在请求时直接报错。

## TTS 音色

人设使用逻辑音色 `assistant`。`tts.profiles.<名称>.voices` 将逻辑别名映射为提供商音色 ID。旧会话的小何默认 ID 在非火山适配器中映射到当前配置的默认逻辑音色；任意其他 ID 不会自动授权。自定义音色必须由配置明确授权，并由对应提供商账户实际允许。修改别名不会改变车控工具选择或参数。

## 验证

`tests/test_audio_providers.py` 覆盖真实 WAV 字节/格式与 multipart 字段、鉴权和缺配、HTTP 错误、无效文本、取消上传、资源关闭、PCM 格式、逻辑音色与真实对齐。测试音频和响应均是显式离线夹具，不代表真实识别或语音合成成功。现有 ASR/TTS/VAD/音频端点和打断测试继续运行。

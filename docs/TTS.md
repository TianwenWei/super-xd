# 火山 Speech 小何 2.0

当前浏览器朗读使用真实火山 Speech V3 双向 WebSocket，固定 speaker `zh_female_xiaohe_uranus_bigtts`、资源 `seed-tts-2.0`。服务端收到 PCM 即逐包转发，浏览器 AudioWorklet 增量渲染，无需等待整段完成。没有 `speechSynthesis`、其他声音、静态缓存或占位音频回退。[官方双向 V3 协议](https://docs.volcengine.com/docs/DoubaoVoice/WebSocketBidirectionalStreaming-V3?lang=zh)、[官方音色表](https://www.volcengine.com/docs/6561/1257544?lang=zh)。

最新核心目标是**每次浏览器提交 query→首个真实非零 PCM ≤1500ms**，取代此前2秒门槛；全部提交作为分母，任一次超标、失败或缺音频都不能宣称全请求达标。浏览器首次真实渲染另报，服务商首包仅用于诊断。当前预算与历史结果见[音频验收](AUDIO_VALIDATION.md)。

默认输出按口语组织：通常1–3句、数十字，实际车控结果一句、任务接手一句、澄清一个关键问。朗读前清除Markdown/URL等显示语法，单位改为自然读法；不是静态应答，不改变原始工具回执与完整模型轨迹。研究任务优先使用同次模型返回的spoken_summary，完整报告与来源在任务卡中查看；未核实与未执行等必要限制仍保留。调整前17次/15次音频失败证据保留，口语专项单列。

简短由生成提示约束，服务端不按前几句、固定字数、关键词或首问截断内容。澄清保留完整返回内容，包括后续问题中的条件；旧报告缺spoken_summary时保留完整事实。清理显示格式时先按原行结构处理表格/链接，再渲染风格，不用删事实换取短音频。用户明确要求详读时保留完整报告。

历史口语专项按当时2秒统计为3/7，其中最新版本4次为1/4；按最新1500ms门槛重评分别为0/7、0/4，没有新测量。实际温控10字，研究同次返回58字spoken_summary与678字完整报告；短表达没有消除模型3–4秒的等待。中间3条与最新4条分开，原17条失败也继续保留。[浏览器专项与阶段证据](AUDIO_VALIDATION.md)。

给定TTS400ms、prefill500ms与200token/s，预算为`500 + 5×N_safe + harness/传输 + 400 ≤1500ms`；N_safe含安全路由前全部生成token，包括工具名、完整合法参数和协议结构。40–60个理想预算token的模型+TTS基线为1100–1200ms，余400–300ms；这是业务预算而非供应商保证，不按汉字或PCM包数计算token。车控必须等实际回执、委托必须真实登记，chat当前仍等完整无工具消息确认；重叠的建连不重复相加。[三条路径预算](AUDIO_VALIDATION.md#当前15秒目标的业务预算)。

## 主流程与安全时序

上游连接为 `wss://openspeech.bytedance.com/api/v3/tts/bidirection`，音频为 24kHz、单声道、signed 16-bit little-endian PCM。StartConnection / StartSession 握手后发送文本，音频包到达即产出；输入结束和收到 SessionFinished 后才视为该供应商会话完成。正常结束的连接可顺序复用，取消/异常连接关闭后不回池；最多保留两个空闲连接，同一连接不并行运行两个会话。连接预热只完成真实网络握手，不合成声音。

用户启用朗读时解锁 AudioContext 并可预热连接；每轮开始时建立 Speech 会话，与 Talker 模型生成重叠。模型 SSE 文字可以立即显示，但 content 后仍可能出现 tool_calls，所以聊天文字等模型消息结束且确定无工具后才送入 TTS；车控只送实际执行回执后的 persona 模板，委托只送真实任务登记后的接手反馈。当前实现的是供应商 PCM→浏览器的增量传输与播放，**尚未实现 LLM 文字 delta→TTS 的增量优化**，也未增加额外路由模型。

浏览器使用独立 `/ws/{session_id}/audio` 接收 JSON 元数据与带 32 字节 playback_id 的二进制 PCM，控制消息仍经原会话 WebSocket。每次播放关联 client_request_id / turn_id / playback_id；两个连接的交叉消息不能假设到达顺序。audio.ended 表示供应商已结束，Worklet 排空后才发送 playback(false)，后台结果需等待当前生成和实际队列播放结束。

停止、新输入、关闭朗读、最后一个音频/控制客户端断线会关闭当前合成并清空 Worklet。旧帧、旧播放状态与错误 ID 不覆盖新轮次。音频断线不取消模型回答或后台任务。零音频、错误帧、供应商超时和部分合成后的错误均明确失败；已听到部分声音也不冒充完整成功。PCM 字节不写入轨迹 JSON，轨迹记录关联、状态和诊断。

## 凭证

新版控制台配置独立 `VOLCENGINE_SPEECH_API_KEY`，使用 `X-Api-Key`；旧版同时配置 `VOLCENGINE_SPEECH_APP_ID` 与 `VOLCENGINE_SPEECH_ACCESS_TOKEN`。两种方式都指定固定 Speech 资源及独立连接/请求 ID；新版 Key 优先。Ark 文本模型凭证不会自动用于 Speech。`.env.example` 提供配置字段和 `VOLCENGINE_SPEECH_TTS_TIMEOUT_SECONDS=45`。[官方鉴权与协议](https://docs.volcengine.com/docs/DoubaoVoice/WebSocketBidirectionalStreaming-V3?lang=zh)。

也可将 `VOLCENGINE_SPEECH_CONFIG_FILE` 指向用户授权的外部 `Secrets.xcconfig`。适配器只读取静态 `TTS_API_KEY` 与 `TTS_RESOURCE_ID`，不执行文件、不处理 include/变量展开，不修改来源。资源必须为 `seed-tts-2.0`；缺字段、占位符、动态引用、重复字段明确拒绝。优先级为环境新版 Key、完整旧版凭证、最后才读配置文件。密钥仅驻留服务端，不进入前端或日志。

配置状态仅表示字段齐全，不能证明账号权限/额度或音色可用。缺配置时文字功能仍可用，朗读明确报错，无替代声音。voice 可省略或使用固定 ID / `xiaohe2.0` / `小何2.0`，其他音色拒绝。文本总上限 4000 字，空白输入拒绝。

## 适配器与计时

`backend/tts.py` 与 `backend/speech_stream.py` 提供：

| 接口 | 行为 |
|---|---|
| `VolcanoTTS(..., ws_connect=None)` | 可明确注入测试网络客户端，运行时连接真实 Speech |
| `.info()` | 配置、来源、固定声音、stream_pool_ready / stream_prewarming；无秘密 |
| `await .prewarm_stream()` | 真实连接握手状态与诊断，不合成占位音频 |
| `.stream_synthesize(texts, on_event=None, voice=None)` | 消费 async 文本迭代器，增量 yield audio bytes 或协议 event / metrics |
| `await .synthesize_with_metrics(text, voice=None)` | 兼容的完整 MP3 返回及 HTTP 首包/总耗时 |
| `await .aclose()` | 关闭活动会话、预热/空闲 WebSocket 和 HTTP 客户端 |

PCM audio 项含 audio bytes、sample_rate、channels、format、nonzero 及阶段指标；event 项含真实协议事件与 metrics。`first_packet_ms` / `first_nonzero_packet_ms` 从 SpeechStream 会话开始计时，不包含租用连接前的等待。`lease_wait_ms`、`prewarm_wait_ms` 单列等待；`adapter_first_packet_ms` / `adapter_first_nonzero_packet_ms` 与 adapter_elapsed_ms 覆盖适配器入口以来的等待。first_text_to_audio_ms 只覆盖首文本进入供应商至音频返回。它们均不是 query→浏览器端到端时间。

浏览器的 submit_perf_ms 与 first_nonzero_pcm_perf_ms 同属页面 performance.now 时间域；合法 PCM 中首次出现非零 int16 样本时记到达时间。空/静音包与元数据不能计首个有效音频。first_play_scheduled 只是向 Worklet 入队；first_nonzero_render_observed 是 Worklet 首次消费非零真实样本后主线程收到通知的观测，包含线程消息延迟，不能当作扬声器声学起点。[Web Audio 时钟与输出时间定义](https://www.w3.org/TR/webaudio/)。

## 验证与历史记录

离线回归明确注入 fake 网络客户端，覆盖二进制协议分片、PCM 对齐、错误/取消关闭连接、干净会话顺序复用、预热等待、独立计时、actor 不阻塞、回执门控、旧轮次隔离及真实 ASGI 路由。测试字节只证明协议和顺序，不能证明真实声音或实际延迟。

```bash
.venv/bin/python -m pytest -q tests/test_speech_stream.py tests/test_audio_session.py tests/test_audio_endpoint.py tests/test_audio_benchmark.py
# 有费用/网络的适配器专项，不替代浏览器端到端验收
.venv/bin/python -m scripts.probe_tts_ws
# 默认1500ms；历史2000ms需显式--target-ms 2000，输出新文件
.venv/bin/python -m scripts.audio_benchmark --input browser-audio.json --manifest audio-manifest.json --output docs/audio-latency-new-run.json
```

真实双向专项两次在文本输入仍开放时收到非零 PCM，Speech adapter→服务端首帧为 672.5 / 710.4 ms，正常完成且连接复用；[原始记录](tts-ws-live.json)保留当时实际字段，不补造后来新增指标。这不是 query→浏览器测量。

旧 HTTP Chunked MP3 接口 `/api/sessions/{session_id}/tts` 兼容保留，服务端收齐后返回，当前 UI 主流程不使用它。旧版三次供应商首包1150.6/535.8/550.0ms、完整MP3合成1335.6/932.0/1165.2ms见[tts-live.json](tts-live.json)；旧 Chrome 长段落开播超过7秒的记录继续保留在[UI验证](../frontend/UI_VALIDATION.md)。这些历史数据不并入当前 PCM 批次，也不能证明新的1500ms端到端目标。

# 火山流式 ASR

页面语音入口统一为[免按键持续监听](VAD.md)：点击输入框旁麦克风即开启连续 VAD 对话，说话停顿后自动提交完整转写；助手回复期间继续监听。再次点击麦克风停止整个连续会话。识别中的文字只作预览，不能下发车控、创建任务或进入模型。取消、断线、识别失败与空结果都不提交。

检测到新语音时打断旧轮次的模型和音频。VAD结束一段只终结该段ASR，麦克风保持开启；收到完整终结标记后才提交一次。文字草稿与识别预览独立，语音发送保留草稿。编辑或发送文字会停止连续监听。连续模式的分段上限和参数见VAD文档；保留的手动`/asr`接口及其`StreamingRecognition`客户端供兼容和独立验收使用，不再从页面麦克风按钮启动。

## 服务端配置

本次找到此前“创建阿拉伯语ASR测试项目”使用的配置文件，并在本项目本地 `.env` 设置只读引用：

```dotenv
VOLCENGINE_ASR_CONFIG_FILE=/absolute/path/to/previous-project/.env
```

只读取独立 ASR Key / App ID / Access Token / Resource ID；支持旧项目的 `VOLC_ASR_API_KEY`、`VOLC_ASR_RESOURCE_ID` 字段。原文件不修改，密钥不复制到前端、文档或测量报告。旧项目的阿拉伯语 language、endpoint、TTS 字段不会被继承。

也可直接设置独立凭证（环境字段优先于引用文件）：

```dotenv
VOLCENGINE_ASR_API_KEY=...
VOLCENGINE_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
VOLCENGINE_ASR_TIMEOUT_SECONDS=15
```

已开通的 1.0 资源 `volc.bigasr.sauc.duration` 也受支持；本机真实验证使用的是这个已有资源。旧版认证可改用 `VOLCENGINE_ASR_APP_ID` 与 `VOLCENGINE_ASR_ACCESS_TOKEN`。独立 ASR Key 与 Ark、TTS Key 分开；配置状态不代表账号资源一定可调用，供应商拒绝会明确显示失败。

服务端连接火山 `bigmodel_async` 双向流式接口，接收浏览器 16kHz / mono / signed 16-bit little-endian PCM。浏览器按实际 AudioContext 采样率重采样并分帧，不能假设麦克风约束一定被设备采用。连续模式每帧32ms；保留的手动客户端每帧100ms，手动接口服务端上限200ms。凭证只用于服务端握手。协议依据[火山大模型流式语音识别文档](https://docs.volcengine.com/docs/6561/1354869?lang=zh)。

## 验证

- 后端 ASR 协议与接口专项 67 项通过：独立凭证、文件配置优先级、二进制协议、分段稳定与完整终结区别、取消与迟到结果、发送失败、异常帧、录音占位与后台播报仲裁。
- [真实云端记录](asr-live.json)：小何合成 3.787 秒中文音频，经已有独立 ASR 凭证识别为“你好，请把空调温度设为23度。”；7 次增量文本与 1 次完整 final，停止调用到 final 消费完成约 409.4ms。该时间包括本地连接收尾，不代表稳定延迟保证。
- [运行服务链路记录](asr-service-live.json)：同一测试音频经 Vite 5173 代理、FastAPI ASR WebSocket 与真实火山识别，7 次 partial 与正确 final，停止到客户端 final 约207.0ms；未发送 input 时创建0个轮次、车况不变。
- 全套后端回归在接入时为 626 passed in 6.33s，含原有车控、任务与 TTS；后续前端及运行服务验证单独记录。
- 前端30项测试及TypeScript/Vite构建通过，包括44.1/48kHz实际采样率重采样、末帧flush、取消、迟到权限/结果、final只发送一次及取消期间的TTS启用竞争。隐藏浏览器新会话确认“会话在线”与麦克风按钮启用；未操作实体麦克风。

复现真实识别会产生正常 ASR 调用；首次运行还会合成一段短小何测试语音，后续复用本地 fixture：

```bash
.venv/bin/python -m scripts.probe_asr --output docs/asr-live-new.json
```

这是手动录音链路的合成语音验证，未验证用户的实体麦克风、声学回声或噪声环境。浏览器采集仍需用户授予权限，当前用本机 localhost 页面；没有接入豆包输入法内部接口或唤醒词。常开监听、VAD 自动结束与播放期间打断见 [VAD.md](VAD.md)。已有 query → 首个非零回复 PCM 的测量继续从最终文本提交开始，录音及 ASR 耗时独立记录在 `recognition_records`，不混入或改写历史延迟样本。

# 火山流式 ASR

页面语音入口统一为[免按键持续监听](VAD.md)：页面打开时不收音，点击麦克风后申请权限并启动连续对话，说话停顿后自动提交完整转写；助手回复期间继续监听。再次点击同一麦克风退出连续监听并释放采集，启动等待期间也可取消。识别中的文字只作预览，不能下发车控、创建任务或进入模型。取消、断线、识别失败与空结果都不提交。

检测到新语音时只停止旧播报，保留旧轮次的模型、查询和已下发控制；只有完整识别 final 提交为新 input 后才打断旧轮次。VAD结束一段只终结该段ASR，麦克风保持开启；收到完整终结标记后才提交一次。文字草稿与识别预览独立，语音发送保留草稿。编辑或发送文字仅丢弃当前未提交的语音片段，麦克风与连续上传保持开启；迟到的旧 final 不会覆盖文字或新语音。断线恢复会按已保存的语音意图重启，显式停止则保持关闭。连续模式的分段上限和参数见VAD文档；保留的手动`/asr`接口及其`StreamingRecognition`客户端供兼容和独立验收使用，不再从页面麦克风按钮启动。

## 服务端配置

2026-10-17 更新：本机旧 ASR Key 对 `volc.bigasr.sauc.duration` 和 `volc.seedasr.sauc.duration` 都返回 HTTP 403，脱敏响应为 `requested resource not granted`。用户确认后，为当前账号的 default 应用（APP ID `8950247596`）开通流式识别 2.0 小时版试用，页面显示 20 小时、3 并发，到期 2027-04-17。现在两套本地服务使用从该 **ASR 服务页面**读取的 APP ID / Access Token 与 `volc.seedasr.sauc.duration`，已真实握手及三轮转写验证。没有把 TTS Key 当作 ASR 凭证，也没有修改原项目的外部配置文件。

本地 `.env` 显式配置 `VOLCENGINE_ASR_APP_ID`、`VOLCENGINE_ASR_ACCESS_TOKEN`、`VOLCENGINE_ASR_RESOURCE_ID`，将 `VOLCENGINE_ASR_API_KEY` 置空；忽略的 `data/services.local.toml` 为 ASR profile 显式指定对应环境变量。配置文件权限为 0600，不提交认证信息。以下只读引用配置说明保留供其他环境使用，不代表本机当前启用的认证方式。

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

离线回归覆盖独立凭证、二进制协议、partial/final、取消与迟到结果、发送失败、录音占位与后台播报仲裁。前端检查实际采样率重采样、末帧flush、权限迟到、final只发送一次与声音解锁竞争。真实语音需另测实体麦克风、环境噪声、AEC及扬声器，历史测试数量或合成输入成功不能代替当前验收。

生成报告保存到忽略的 `data/validation/`，不提交完整语音/对话轨迹；采样口径见[音频验收](AUDIO_VALIDATION.md)。

复现真实识别会产生正常 ASR 调用；首次运行还会合成一段短小何测试语音，后续复用本地 fixture：

```bash
.venv/bin/python -m scripts.probe_asr --output data/validation/asr-live-new.json
```

这是手动录音链路的合成语音验证，未验证用户的实体麦克风、声学回声或噪声环境。浏览器采集仍需用户授予权限，当前用本机 localhost 页面；没有接入豆包输入法内部接口或唤醒词。常开监听、VAD 自动结束与播放期间打断见 [VAD.md](VAD.md)。已有 query → 首个非零回复 PCM 的测量继续从最终文本提交开始，录音及 ASR 耗时独立记录在 `recognition_records`，不混入或改写历史延迟样本。

连续模式将用户的开启意图与实际 microphone/context/socket ready 分开。浏览器自动播放受限时显示明确操作入口，不能静默切为文字；服务端 ASR 配置存在也不等于调用授权有效，provider 鉴权拒绝会明确提示且不会自动无限重试。朗读偏好可跨刷新及新建会话保留；麦克风开启状态不持久化，每次打开页面均需点击开启。

# 火山流式 ASR

页面语音入口统一为[免按键持续监听](VAD.md)：页面打开时不收音，点击麦克风后申请权限并启动连续对话，说话停顿后自动提交完整转写；助手回复期间继续监听。再次点击同一麦克风退出连续监听并释放采集，启动等待期间也可取消。识别中的文字只作预览，不能下发车控、创建任务或进入模型。取消、断线、识别失败与空结果都不提交。

检测到新语音时只停止旧播报，保留旧轮次的模型、查询和已下发控制；只有完整识别 final 提交为新 input 后才打断旧轮次。VAD结束一段只终结该段ASR，麦克风保持开启；收到完整终结标记后才提交一次。文字草稿与识别预览独立，语音发送保留草稿。编辑或发送文字仅丢弃当前未提交的语音片段，麦克风与连续上传保持开启；迟到的旧 final 不会覆盖文字或新语音。断线恢复会按已保存的语音意图重启，显式停止则保持关闭。连续模式的分段上限和参数见VAD文档。

## 服务端配置

本页描述火山适配器；当前使用哪个ASR由[服务配置](AUDIO_PROVIDERS.md)选择。凭证放在进程环境或本地`.env`，API Key与APP ID / Access Token两种方式按账号实际授权选择。例如使用独立ASR Key：

```dotenv
VOLCENGINE_ASR_API_KEY=...
VOLCENGINE_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
VOLCENGINE_ASR_TIMEOUT_SECONDS=15
```

采用APP ID / Access Token时，设置`VOLCENGINE_ASR_APP_ID`、`VOLCENGINE_ASR_ACCESS_TOKEN`与实际授权的`VOLCENGINE_ASR_RESOURCE_ID`，并清空`VOLCENGINE_ASR_API_KEY`。适配器也支持已授权的1.0资源`volc.bigasr.sauc.duration`。独立ASR凭证与Ark、TTS分开；配置齐全不证明账号已获资源授权，供应商拒绝会明确显示失败。

可选`VOLCENGINE_ASR_CONFIG_FILE=/absolute/path/to/asr.env`只读引用已有配置，环境字段优先。只读取ASR的Key、App ID、Access Token和Resource ID，也识别对应的`VOLC_ASR_*`字段；不继承语言、endpoint或TTS配置，不修改引用文件。

服务端连接火山 `bigmodel_async` 双向流式接口，接收浏览器 16kHz / mono / signed 16-bit little-endian PCM。浏览器按实际 AudioContext 采样率重采样并分帧，不能假设麦克风约束一定被设备采用。持续监听每帧32ms。凭证只用于服务端握手。协议依据[火山大模型流式语音识别文档](https://docs.volcengine.com/docs/6561/1354869?lang=zh)。

## 验证

离线回归覆盖独立凭证、二进制协议、partial/final、取消与迟到结果、发送失败、录音占位与后台播报仲裁。前端检查实际采样率重采样、末帧flush、权限迟到、final只发送一次与声音解锁竞争。真实语音需另测实体麦克风、环境噪声、AEC及扬声器，历史测试数量或合成输入成功不能代替当前验收。

生成报告保存到忽略的 `data/validation/`，不提交完整语音/对话轨迹；采样口径见[音频验收](AUDIO_VALIDATION.md)。

复现真实识别会产生正常 ASR 调用；首次运行还会合成一段短小何测试语音，后续复用本地 fixture：

```bash
.venv/bin/python -m scripts.probe_asr --output data/validation/asr-live-new.json
```

这是直接调用供应商适配器的独立探针，使用合成语音，未验证浏览器端点、实体麦克风、声学回声或噪声环境。浏览器采集仍需用户授予权限，当前用本机 localhost 页面；没有接入豆包输入法内部接口或唤醒词。常开监听、VAD 自动结束与播放期间打断见 [VAD.md](VAD.md)。已有 query → 首个非零回复 PCM 的测量继续从最终文本提交开始，录音及 ASR 耗时独立记录在 `recognition_records`，不混入或改写历史延迟样本。

连续模式将用户的开启意图与实际 microphone/context/socket ready 分开。浏览器自动播放受限时显示明确操作入口，不能静默切为文字；服务端 ASR 配置存在也不等于调用授权有效，provider 鉴权拒绝会明确提示且不会自动无限重试。朗读偏好可跨刷新及新建会话保留；麦克风开启状态不持久化，每次打开页面均需点击开启。

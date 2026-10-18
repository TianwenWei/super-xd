# 免按键语音与本地 VAD

页面打开时麦克风关闭。会话与 ASR/TTS/VAD 配置就绪后，点击麦克风一次，在原始手势内申请麦克风并解锁声音，进入免按键持续对话；启动中可再次点击取消，启动失败显示重试入口。模式意图与实际就绪分开，只有采集与监听连接真正就绪才显示持续监听。开启后，助手回复播放期间仍持续上传麦克风 PCM，说话停顿后自动发送，下一轮无需再点击。

再次点击同一个麦克风或“关闭连续语音”会停止监听并释放麦克风，未提交片段与迟到 final 都不发送；编辑/发送文字只丢弃当前未提交语音片段，监听继续。控制连接断开时释放旧采集，恢复后仅延续本页已主动开启且未关闭的模式；显式关闭后不会自动重启。朗读偏好存于标签页，麦克风开启状态不持久化，新建会话或刷新后重新点击开启。浏览器暂停采集 context 时会尝试恢复，无法恢复明确显示一键入口。供应商拒绝/鉴权失败明确分类，不自动无限重试；修复服务后可手动重试。

本地运行的是 **Silero VAD v5.1.2 ONNX**，使用 CPU 单线程。ASR、Talker、TTS使用启动时选定的[服务配置](AUDIO_PROVIDERS.md)。浏览器通过 `/ws/{session_id}/listen` 持续发送 16kHz / mono / s16le PCM，每帧 512 samples（32ms）；后端在线程中执行推理，每个监听连接独立持有模型循环状态。

默认参数：连续 3 帧语音概率 ≥ 0.6 后开始一段（96ms 确认窗口）；段内概率 < 0.35 累计 20 帧后结束（640ms 静音）；保留 10 帧开头音频（320ms，包含确认帧）。这些是声学分段参数，不代表模型理解了用户是否说完。连续说话超过 55 秒会停止本次监听并明确报错，不自动提交截断内容。

开始说话时，服务端向会话 actor 投递带 `interrupt: true` 的 `asr.capture`，actor **只停止旧轮次的播报**；前端收到 `voice.speech_start` 后清空播放 worklet 缓冲。旧轮次的模型、规划、查询和已下发的车控继续进行：VAD 区分不了说话、咳嗽、路噪和扬声器回声，在这里取消轮次会让已下发的车控被模拟器判为失效。只有识别出完整的一句话、作为 `input` 提交后，新一轮才按原有规则打断旧轮次。

`voice.speech_end` 只终结这一段 ASR，不关闭麦克风或监听连接。只有当前 capture 的完整 `voice.final` 才提交一次现有 `input`。新语音在旧 final 等待期间到来，会取消旧 ASR 并更换 capture ID。等待播放进度回报后发送 input 时，还会再次检查输入版本；旧识别不能覆盖新一段语音。ASR、TTS 失败时明确报错，无替代回复。

浏览器请求 `echoCancellation`、`noiseSuppression`、`autoGainControl`。**VAD 不能区分用户、电视和扬声器里的其他人声，也不能代替回声消除。** 当前是全双工交互原型；推荐先用耳机验收，再测试笔记本扬声器和实体麦克风。合成音频测试不证明实际声学环境下没有回声误触发。

## 安装与复现

模型保存在 `data/models/silero-vad/silero_vad_v5.onnx`，来源为[官方 v5.1.2 发布版本](https://github.com/snakers4/silero-vad/tree/v5.1.2)，采用 MIT 许可。下载脚本记录来源和 SHA256 到 `data/models/manifest.json`。权重、数据库、运行报告不提交 Git。

```bash
uv sync --locked --extra demo --extra local-vad
.venv/bin/python -m scripts.setup_local_models
.venv/bin/python -m scripts.probe_local_vad
.venv/bin/python -m scripts.probe_local_vad --live --out data/validation/local-vad-live-new.json
```

最后两条命令默认复用`data/asr-chinese-probe.pcm`小何合成音频；文件缺失时先运行`.venv/bin/python -m scripts.probe_asr`生成。`--live`要求显式选中火山ASR/TTS，并使用当前配置的模型角色，验证首段final→回复PCM→第二段语音打断旧回复，输出脱敏trace。它不使用实体麦克风、不测试浏览器AEC，也不模拟车控执行回执。

## 验证边界

离线回归覆盖32ms分帧、取消后的迟到权限、旧final丢弃、重复final、空结果、连接积压和新语音期间的旧回执隔离。App接线测试还核对监听意图与ready状态、文字编辑不关闭持续监听、重连恢复、显式停止、音频解锁与采集context恢复。

VAD探针分别报告模型加载和热态推理耗时；这些指标不包含采集、分帧、96ms确认窗口、WebSocket、线程调度或云端延迟。完整语音交互须按[音频验收](AUDIO_VALIDATION.md)另测实体麦克风、扬声器AEC与实际供应商链路。每次测量保留配置、版本、失败样本及脱敏trace到`data/validation/`，不把历史测试数量或合成音频成功当作当前验收。

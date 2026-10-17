# 免按键语音与本地 VAD

页面默认选择免按键持续对话和语音回复；会话与 ASR/TTS/VAD 配置就绪后会自动尝试启动。已有浏览器授权时自动进入，首次麦克风权限或 AudioContext 声音解锁受限时明确显示“进入语音对话”，点击一次在原始手势内申请麦克风并解锁声音。模式意图与实际就绪分开，不能因为默认选择开启就显示已经监听。允许后，助手回复播放期间仍持续上传麦克风 PCM，说话停顿后自动发送。

再次点击停止图标或“关闭连续语音”会停止监听并释放麦克风；编辑/发送文字只丢弃当前未提交语音片段，监听继续且迟到 final 失效。控制连接断开时释放旧采集，恢复后按原意图重启；显式关闭的模式不会在重连中偷偷开启。语音与朗读偏好存于标签页，新建会话保留选择，无保存记录时默认开启。浏览器暂停采集 context 时会尝试恢复，无法恢复明确显示一键入口。供应商拒绝/鉴权失败明确分类，不自动无限重试；修复服务后可手动重试。

本地运行的是 **Silero VAD v5.1.2 ONNX**，使用 CPU 单线程。ASR、Talker、TTS 继续使用现有火山服务。浏览器通过 `/ws/{session_id}/listen` 持续发送 16kHz / mono / s16le PCM，每帧 512 samples（32ms）；后端在线程中执行推理，每个监听连接独立持有模型循环状态。

默认参数：连续 3 帧语音概率 ≥ 0.6 后开始一段（96ms 确认窗口）；段内概率 < 0.35 累计 20 帧后结束（640ms 静音）；保留 10 帧开头音频（320ms，包含确认帧）。这些是声学分段参数，不代表模型理解了用户是否说完。连续说话超过 55 秒会停止本次监听并明确报错，不自动提交截断内容。

开始说话时，服务端向会话 actor 投递带 `interrupt: true` 的 `asr.capture`，actor **只停止旧轮次的播报**；前端收到 `voice.speech_start` 后清空播放 worklet 缓冲。旧轮次的模型、规划、查询和已下发的车控继续进行：VAD 区分不了说话、咳嗽、路噪和扬声器回声，在这里取消轮次会让已下发的车控被模拟器判为失效。只有识别出完整的一句话、作为 `input` 提交后，新一轮才按原有规则打断旧轮次。

`voice.speech_end` 只终结这一段 ASR，不关闭麦克风或监听连接。只有当前 capture 的完整 `voice.final` 才提交一次现有 `input`。新语音在旧 final 等待期间到来，会取消旧 ASR 并更换 capture ID。等待播放进度回报后发送 input 时，还会再次检查输入版本；旧识别不能覆盖新一段语音。ASR、TTS 失败时明确报错，无替代回复。

浏览器请求 `echoCancellation`、`noiseSuppression`、`autoGainControl`。**VAD 不能区分用户、电视和扬声器里的其他人声，也不能代替回声消除。** 当前是全双工交互原型；推荐先用耳机验收，再测试笔记本扬声器和实体麦克风。合成音频测试不证明实际声学环境下没有回声误触发。

## 安装与复现

模型保存在 `data/models/silero-vad/silero_vad_v5.onnx`，来源为[官方 v5.1.2 发布版本](https://github.com/snakers4/silero-vad/tree/v5.1.2)，采用 MIT 许可。下载脚本记录来源和 SHA256 到 `data/models/manifest.json`。权重、数据库、运行报告不提交 Git。

```bash
uv pip install --python .venv/bin/python -r requirements.local-models.txt
.venv/bin/python -m scripts.setup_local_models
.venv/bin/python -m scripts.probe_local_vad
.venv/bin/python -m scripts.probe_local_vad --live --out data/local-vad-live.json
```

最后两条命令默认复用此前生成的 `data/asr-chinese-probe.pcm` 小何合成音频；若该文件缺失，可先运行 `scripts.probe_asr` 生成。`--live` 会调用真实火山 ASR / Talker / TTS，验证首段 final → 回复 PCM → 第二段语音打断旧回复，并输出脱敏 trace。它不使用实体麦克风、不测试浏览器 AEC，也不模拟车控执行回执。

本机 M3 Pro / 18GB 的一次实际测量：模型加载 151.8ms；151 帧中排除首帧后，推理 P50 0.093ms、P90 0.100ms、P99 0.110ms。仅为热态模型调用耗时，不包括音频采集、分帧、96ms 确认窗口、WebSocket、线程调度或云端延迟。

## 本次验证

- 后端 1021 项通过。其中 1020 项在沙箱内通过，1 项真实本机 TCP 监听测试在允许回环网络后单独通过。
- 前端 48 项音频/识别回归、32ms 分帧及 TypeScript/Vite 构建通过。包含取消后的迟到权限、持续采集、旧 final 丢弃、重复 final、空结果、连接积压和新语音期间的旧回执隔离。
- 真实火山链路报告：`data/local-vad-live.json`；两次识别均返回“你好，请把空调温度设为23度。”，首段产生真实非零 TTS PCM，第二段开始时旧回复被取消，捕获标识已更新。
- 抽查 `data/local-vad-live.trace.json`：`vad.speech_started` → `vad.speech_ended` → `asr.completed` → `turn.started` → `model.started/completed` → `audio.text` → `tool.started` / `vehicle.dispatched` → 下一段 `vad.speech_started` → `audio.cancelled` / `turn.interrupted`。模型输入、输出、调用都保留在原有 trace；没有给待执行车控编造成功回执。

完整模型评测各重复三次。基线在独立进程内还原本次 SessionActor 的中断改动，保留此前工作区已有改动，没有覆盖文件。两组均使用现有 prompt 和相同用例；文字评测不覆盖声学回声。

| 指标 | 本次改动前逻辑 | 本次改动后 |
|---|---:|---:|
| 用例通过 | 140/144（97.22%） | 141/144（97.92%） |
| 首句可播 P50 | 874ms | 1468ms |
| 首句可播 P90 | 2360ms | 5384ms |
| 平均 Talker 调用次数 | 1.16 | 1.18 |

报告分别在 `data/evals/local-voice-before-live.json` 和 `data/evals/local-voice-regression.json`。没有新增失败的用例 ID；`hold_ambient_not_closed` 在两组均为 0/3，属于已有车控保持问题；`correction_last_wins` 基线 2/3，本次 3/3。延迟变慢已保留原始数据，不能据此宣称优化：两组分时运行、共享云端服务且有模型采样/缓存差异。这次没有增加串行模型调用、修改路由或 prompt。未运行话术审查，因为没有修改模型话术模板或示例。

Embedding 按当前决定使用豆包在线模型，等待指定模型/接入点 ID；没有把本地 BGE 或检索逻辑接入 Talker。

## 默认模式前端修复验证

新增真实 App 接线测试先复现默认关闭、文字销毁监听、重连不恢复和 VAD 提前作废轮次，再验证修复。测试还覆盖默认意图不虚报 ready、final 启用 TTS、鉴权错误无重试循环、显式停止保留、设备查询身份仍有效、浏览器自动播放被阻止的明确入口，以及采集 context 暂停恢复。隔离媒体/协议测试不替代实体麦克风、扬声器 AEC 与真实供应商授权验证。

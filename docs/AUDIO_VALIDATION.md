# 音频验收方法

浏览器主要指标为同页 `performance.now` 时间域的 **query提交→首个合法且含非零样本的PCM包到达**，目标≤1500ms；目标常量为 `frontend/src/performanceTarget.ts`。所有提交都进入分母，失败、取消、无音频及超时分别列出，不能只报成功样本P50或删除慢样本。主动任务播报没有query提交起点，单独统计。

## 分层记录

| 指标 | 边界 |
| --- | --- |
| query→首非零PCM | 浏览器提交至收到真实有效音频；主指标 |
| 首PCM入队 | 向AudioWorklet写入，不代表播放 |
| 首样本/首非零渲染通知 | Worklet实际消费后的主线程观察，含跨线程通知延迟 |
| provider首包/连接复用/首文本至音频 | Speech适配器诊断，不包含全部前端及模型耗时 |
| 程序WebSocket收到PCM | 程序时钟测量，不能替代浏览器或实体扬声器 |

浏览器通知不是扬声器声学起点。空帧、静音帧、状态元信息不能计首有效音频；ASR从采集到final另计，不混入已提交query指标。记录实际模型、usage/cache（若提供）、冷暖连接、并发负载、设备回执、后台任务和取消原因，不凭字符数推断token。

## 采样与断言

覆盖闲聊、热车控、复杂车控、查询、任务登记/回流、拒绝与澄清；同时覆盖输入期间流播、用户打断、错误ID/迟到PCM、双WebSocket乱序、断线、TTS部分输出后失败和后台播报等候自然drain。明确标记故意取消场景，保留全量与预先定义子集，不能事后筛选成功率。

前瞻预告可以随模型文本流播；完整工具组验证前不能下发。预告不能宣称已完成；确定成功已有预告时不重复播报，失败和unknown必须反馈。无预告时核对一次dispatch反馈或实际完成反馈，任务登记必须有真实账本确认。模型原文、实际audio.text、完整任务报告和证据分别核验。

`scripts.audio_benchmark` 不再把所有回执前音频视为违规。若首音频早于车控回执或任务登记，必须把实际先播文本放进 `pre_receipt_spoken_text`，另附人工或模型语义审阅：`pre_receipt_speech_review` 包含 `status="reviewed"`、`kind="progress"`、同一 `turn_id`、`reviewer="human"|"model"`、与前述文本完全一致的 `text`，以及覆盖到回执时点的 `through_perf_ms`。缺审阅、错轮次或文本不一致仍不能通过。代码只验证审阅与本轮证据的绑定，不用关键词判断时态；真实回执、目标状态和任务登记仍单独校验。若记录了 `completion_claim_perf_ms`，完成宣称不得早于实际回执。这项审阅不由浏览器自动填写，也不能用首字时间代替音频。

```sh
.venv/bin/python -m pytest -q tests/demo/test_speech_stream.py tests/demo/test_audio_session.py tests/demo/test_audio_endpoint.py tests/evals/test_audio_benchmark.py
.venv/bin/python -m scripts.audio_benchmark --plan
.venv/bin/python -m scripts.audio_benchmark --input data/validation/browser-audio.json --manifest data/validation/audio-manifest.json --output data/validation/audio-report.json
```

浏览器导出、场景manifest、完整trace和截图保存到本机忽略目录。输出使用新文件名，不覆盖原批次；真实外部模型/TTS请求由执行者明确启动。报告列出全部请求、通过比例、P50/P90/P95、最大值及缺失项，并把语义/回执正确与延迟达标分别判定。旧版本样本不能证明当前版本通过；压缩文字也不能直接证明首PCM变快。

协议与配置见[TTS](TTS.md)、[Demo接口](INTERFACES.md)，整体门槛见[当前验收方法](VALIDATION.md)。

# 独立并发与状态审查

审查范围：会话 actor、任务 revision/inbox、Thinker 上下文、迟到事件、取消与火山 TTS/前端播放仲裁。以下三项在当前实现中已关闭；保留修复前复现，便于追溯原因与验证范围。

本次审查未修改被审查代码。修复前复现使用 `/tmp` 隔离 SQLite 数据库及明确注入的测试 Ark 客户端，只用于确定竞态。收尾复核基于当前代码和已有回归测试；正式 40 样本采集前主批次为 **421 passed**，导航修复后的最终离线结果由主实现报告为 **431 passed in 4.26s**。没有为本文扩展实测。真实模型、Tavily、TTS 与浏览器记录见各自报告，不将注入客户端测试当作真实服务验收。

## 1. 旧 revision 的模型异常会把新 revision 标成失败（P1，已关闭）

位置：`backend/worker.py` 的 `run_task`、`_run_task`、`_fail_revision`；`backend/db.py` 的 `save_task`。

修复前：revision 1 的 `Ark.complete` 正在等待时，用户更新到 revision 2；旧请求随后抛出网络异常。原通用异常分支调用 `_fail_current`，读取最新任务并把旧异常写为新 revision 的失败。

修复前隔离复现的实际结果：

```json
{"revision":2,"status":"failed","constraints":["只考虑室内"],"error":"RuntimeError: old revision network failure"}
```

当前实现：一般模型异常绑定产生它的 revision，`_fail_revision` 在边界与写入时检查版本。版本已变化时丢弃旧异常，并重建新条件下的模型上下文。工具结果和错误写回同样受 revision 检查约束。绝对时间截止与全任务步骤上限仍作用于整个任务，不因更新重置。

已有验证（[worker 回归](../tests/test_worker.py)）：

- `test_old_model_error_cannot_fail_new_revision`：旧模型异常后 revision 2 正常完成，无旧错误、无失败事件，已花费的步骤仍计数。
- `test_current_model_error_fails_only_its_own_revision`：当前版本的模型异常仍正确结束当前任务。
- `test_revision_discards_late_search_and_rebuilds_with_new_condition`：旧搜索结果不进入新条件上下文，新版本按更新后的条件完成。

## 2. 澄清答复丢失对应问句（P2，已关闭）

位置：`backend/db.py` 的 `mutate_task`；`backend/worker.py` 的 `_messages`。

修复前：回复进入 `constraints` 后清空 `question`，重建消息不携带原问句。先问“可以接受步行十分钟吗？”，再回复“可以”，新 revision 的模型输入为：

```json
{"contains_original_question":false,"constraints":["可以"]}
```

当前实现：清空待回答问句前，把 `question`、`answer` 和新 revision 写入 `context.clarification_history`；worker 重建消息时携带这个中性 context。登记时的车况和近期历史保留，原任务 ID、累计步骤与硬截止不重置。

已有验证（[澄清上下文回归](../tests/test_clarification_context.py)）：`test_brief_reply_keeps_original_question_and_initial_vehicle_context` 用“可以”作为短答，检查保存的问答对、revision 2、原始车窗状态，并直接检查 worker 的模型输入同时包含原问句与答复。

## 3. 断线播放状态滞留及其他客户端新轮次未停止旧音频（P2，已关闭）

位置：`frontend/src/App.tsx` 的连接/播放处理和 `turn.started` 分支；`backend/session.py` 的 `_attach`、`_detach`、`announce`。

修复前：浏览器报告 `playback: true` 后断线，期间音频结束，结束消息无法送达；重连未同步空闲，主动播报被旧占位长期阻止。修复前隔离 actor 复现：

```json
{"speaking_after_reconnect":true,"pending_announcements":1,"proactive_started_after_reconnect":0}
```

另一个触发路径是同会话其他客户端发起新轮次：上一轮文字已经完成、MP3 仍在播放时，原前端仅依赖本地提交或旧轮次的 `stream.interrupted` 停止音频，不能覆盖该路径。

当前实现：前端断线立即停止音频与合成请求，重连发送播放空闲；所有有效 `turn.started` 统一停止旧音频。最后一个客户端离开时，actor 清除播放占位并取消该轮音频；没有客户端时保留待播结果与未消费的播报标记，重新 attach 后再次仲裁。

已有验证：

- [会话回归](../tests/test_session.py) `test_disconnect_releases_playback_and_pending_result_waits_for_reattach` 覆盖断线释放占位、离线期间后台完成、待播结果保留和重连后播报。
- [TTS 端点回归](../tests/test_tts_endpoint.py) 覆盖新输入及显式停止取消服务端合成、迟到音频不返回，以及禁止朗读任意或未完成文本。
- [真实浏览器记录](../frontend/UI_VALIDATION.md) 覆盖小何 MP3 播放、停止、播放中与合成中发起新轮次，以及后台完成等待正在播放的音频自然结束后回流。

关闭结论基于修复代码、上述自动回归与相关真实播放记录；没有把跨标签新输入或断线场景夸称为独立完成的真实浏览器专项。

## 其余已检查链路

- 模型 delta/done 经事件队列进入 actor，旧 turn 的文字输出有 current 检查。
- 已执行车控的迟到回执仍写入对应旧 turn 历史，重复 command ID 不会再次执行；旧 turn 不再生成成功播报。
- 任务事件使用 event ID、revision 和数据库最新任务状态过滤，避免旧卡片覆盖新条件。
- Worker 在外部请求前结束 SQLite 短事务；Talker 与 Thinker 并发额度分离，任务有共享步骤/绝对时间边界。
- 登记任务的 context 包含完整模拟车况、近期对话及活跃任务；Thinker 输出中性结果与建议，建议回流时按最新车况重新校验。
- [最终真实人设报告](persona-live.json) 的 18 轮及 3 次真实车控预置全部通过。三个 persona 的温度、左前车窗、后视镜加热工具/参数/路径一致，语言不同；寒暄及带已执行历史的寒暄没有越权车控或委托。修复前失败证据保存在 `persona-live.before-fix.json` 和 `persona-live.before-strict.json`。

## 导航地点原文保持专项（已关闭，保留原失败）

正式 [40 样本延迟报告](latency-live.json) 中两次导航地点被模型缩写并错误执行的样本继续记为失败，原报告不覆盖、不回算。修复与后续专项按阶段记录：

1. 在 `backend/session.py` 为明确的字面目的地命令增加下发前原文校验，不改写模型参数。第一轮真实专项仍为 **0/3 正确执行**，但三个 persona 生成的“沪”均被拒绝，没有下发命令，目的地保持为空；错执行拦截为 **3/3**。[中间证据](navigation-live.before-pattern-fix.json) 保留原始 `audit_calls`、目的地状态及拒绝结果。
2. 目录自由文本 schema 原先使用裸 `\S`，JSON Schema 将其解释为包含非空白字符；真实输出持续为单字，怀疑服务商 strict 解码按整串匹配导致该限制。当前改为语义等价、显式覆盖完整字符串的 `[\s\S]*\S[\s\S]*`。这是基于代码和输出的原因判断，没有把服务商内部实现当作已证实事实。
3. 修复 pattern 后的[最终真实专项](navigation-live.json) 为 **3/3 正确执行**。每个 persona 使用新会话，模型原始参数均为完整“上海虹桥站”，实际模拟状态提交成功；专业、温暖、活泼的执行回执分别为 **814.2 / 1374.5 / 1359.4 ms**。没有修正或替换模型参数。

已有离线验证：`test_changed_navigation_place_is_rejected_before_dispatch` 覆盖缩写、截断、翻译及额外空白被拒绝且状态不变；`test_navigation_retains_full_literal_place` 覆盖完整站名、带引号名称、门牌地址和数字名称；`test_free_text_patterns_allow_full_place_names_in_strict_decoding` 检查多字、中英文和换行文本的整串匹配，并拒绝纯空白。

专项仅验证上述三个 persona 的明确目的地命令，不合并进原 40 样本统计，也不把模拟导航当作真实路线或地点解析服务。

这些结论是针对已读代码和已列证据的范围判断，不代替整体延迟、所有网络故障或全部浏览器行为验收。

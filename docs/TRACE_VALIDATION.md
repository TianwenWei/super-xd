# 模型输出 / Agent轨迹验收

2026-10-11。本轮增加完整可观测记录，不改变13个固定工具、100个模拟函数、10个skill、persona、任务版本/取消或Speech播放仲裁。

## 界面入口

打开 <http://127.0.0.1:5173>，页头下方有 **“模型输出 / Agent轨迹”** 按钮；每条助手消息及后台任务卡也有同名入口，可直接定位该turn或task。抽屉可以切换全会话、某轮对话、某任务，以及“完整轨迹 / 模型原文”。点击call ID可筛选关联模型和工具事件。

事件按发生时间、trace ID显示，包含稳定的session/turn/task/call ID、revision及step。每条可展开，长文本/多行JSON完整保留；支持复制字段、完整事件或当前筛选的全部记录。打开时分页读取，之后每秒增量更新；关闭保留缓存。默认不跟随最新，增量事件不收起已有展开项、不强制跳阅读位置；用户可以主动开启跟随。桌面和400×741窄屏均验证。

## 实际记录的内容

- 每次Talker和Thinker模型调用的开始、完整返回、错误/取消，以及调用间完整工具参数和实际结果。模型开始记录输入消息和工具定义，便于观察harness上下文。
- Talker保留实际收到的SSE帧并重组content、function名称及完整参数；Thinker保留完整provider响应。各轮独立call ID，元数据不混入下一轮模型消息。
- provider实际返回的ID/model/created/finish_reason/usage，以及本地观测的耗时、首delta时间和接收时间。没有返回的字段保持null/缺失，不用估计值填充。本次真实Talker流未返回usage，Thinker返回了实际usage。
- reasoning_content仅展示provider实际返回的字段，并明确其来源。Talker未返回时说明未返回；不会推导、补写或声称观测到其他内部思考。
- tool-only响应的content=null明确显示，独立于工具结果和persona模板反馈。最终用户反馈在turn.completed单独记录output及output_source，可与模型原文比较。
- 车控下发、实际模拟执行回执及超时；任务创建、运行、澄清、回复、修改、取消、错误、完成及版本弃用事件。旧输出、迟到回执仍留在原turn/task/revision，标记stale/cancelled，不冒充当前回答。

没有采集HTTP认证头、客户端对象或秘密配置；持久化边界递归清除认证字段和已知凭证值。除脱敏内容外，不对正文和工具结果做摘要截断。这是本机调试日志，SQLite中包含用户对话、模型输入/输出和工具返回内容。

## 真实浏览器与API验证

第一次真实CUA会话 `b0cb5edc9aed41f1` 共50事件，完整导出见 [trace-live.before-final-association.json](trace-live.before-final-association.json)，不是固定模型回复或模拟网页数据：

| 记录 | 实际验证 |
|---|---|
| 聊天turn `abe2e65c7c92` | 首有效字约967ms，4事件可回看；复制JSON读回18,616字符 |
| 车控turn `14281cd6177f` | `set_window_position(driver,50)`实际提交，8事件串起模型、参数、工具、车控回执和模板反馈；模型content=null、reasoning未返回，raw_chunks完整 |
| Thinker task `b60ba8f436d4` | 4个模型步骤，真实Search×2、官网Extract、finish；四轮实际reasoning与usage可见，官方来源和查询时间可核对 |
| 长结果 | 任务范围38事件复制读回535,142字符；官网Extract正文3,041字符完整可展开/复制 |
| 实时阅读 | 全会话26→42事件时，旧模型事件保持展开、scrollTop保持0；最终缓存50，未强制跳底 |
| 窄屏 | 400×741下drawer及document均400宽，无横向溢出，长JSON在字段区域滚动 |

上述第一次真实会话发现手工任务错误地挂到上一个车控turn；原始导出保留该事实，未改写历史记录。最终实现仅模型delegate携带来源turn；手工创建任务origin_turn_id为null，以task ID关联。

最终新会话 `1f027f0233104f87` 的完整26事件保存在 [trace-live.json](trace-live.json)，11项API记录核对均通过：聊天单次首有效字1512ms；主驾窗50%的实际执行回执1371.63ms；手工算术任务 `f736f4b860d9` 用一次真实Thinker调用完成39593，并保留实际reasoning、usage及完整provider响应。手工任务的worker/生命周期事件turn_id均为null；最终主动反馈有自己的新turn，正常关联task。三个模型调用ID独立，所有工具开始/结果均关联其模型调用；车控模型content=null与最终persona反馈分开。

最终CUA再次复制核对26事件，刷新后已完成消息和任务恢复、消息入口仍定位对应turn。Chrome桌面留在该在线会话，抽屉保持开启，Thinker模型原文已展开；无需刷新。其他重启前旧标签若会话失效，应点“新建会话”。这两次单次耗时仅作真实功能验证，不用于替换正式分位数。

截图和完整浏览器操作见 [frontend/UI_VALIDATION.md](../frontend/UI_VALIDATION.md)，截图在 `frontend/validation/trace-*.jpg`。

## 必要竞态修复与测试

独立复现发现：新input已经排队、尚未被actor消费时，旧模型完成/异常如果在网络callback中判定状态，可能先记completed/failed，随后实际被actor丢弃。现在完整响应/错误连同原接收时间、meta、call ID一起进入actor队列，在过滤旧turn之前记录，并按实际消费顺序标stale。原始内容仍保留，不影响是否执行工具。旧turn超时回执也统一标late/stale。

最终 `.venv/bin/python -m pytest -q`：**470 passed in 6.27s**。新增针对性覆盖：

- SSE跨chunk及工具参数分片重组、usage-only末帧、完整provider字段、取消半句/错误帧和缺失字段。
- 同turn多次模型调用、双Thinker并发ID隔离、完整长正文/实际返回reasoning、模板与模型原文分离。
- 新输入排队与旧模型完成/错误竞争；已执行/超时的迟到回执；旧任务revision结果可回看但不采用；重复ACK不产生重复执行记录。
- 手工任务无虚假来源turn，模型delegate关联本turn；451条记录跨页无丢失，session/turn/task过滤组合隔离。
- 暂阻SQLite日志写线程时，actor仍在1秒测试上限内返回stream.done，证明日志不阻塞快速路径；写失败会在面板明确警示且不改变聊天结果。

这些是明确注入网络客户端的离线竞态/协议测试，不作为真实模型延迟。没有重跑或覆盖此前40样本主基准。最终 `npm run build`（TypeScript+Vite）通过，30 modules，JS268.23kB（gzip85.05）、CSS35.62kB（gzip8.61）；最终截图为 `frontend/validation/trace-final-ready.jpg`。

## 接口与限制

`GET /api/sessions/{session_id}/traces?after_id=0&turn_id=...&task_id=...&limit=200` 返回完整events、next_after_id、has_more、recording_error。cursor按持久化trace ID推进；显示顺序按created_at与ID。Talker通过独立有序异步写队列存SQLite，Thinker步骤及任务生命周期使用短事务；不持网络事务、不发送大trace到聊天/TTS流。详见 [INTERFACES.md](INTERFACES.md)。

模型生成中先显示开始事件，完整返回在完成/取消后入轨；普通聊天文字仍流式显示，面板不逐token重绘。新采集之前的历史无法补出当时未保存的provider响应或元数据。日志虽保存在SQLite，当前UI/API仍从在线会话进入，没有跨服务重启恢复或历史session浏览器。字段只反映应用实际收到的内容，日志也不能揭示未返回的内部推理。

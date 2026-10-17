# NLP / Gateway V1契约

这是本项目实现的目标集成契约，尚未经外部Gateway团队确认。Python模型位于`backend/harness/gateway.py`、`contracts.py`、`background.py`、`task_transport.py`；HTTP入口位于`backend/nlp_app.py`。`gateway-v1`是业务协议版本。未知字段、错误类型、非有限数值、无时区时间戳均拒绝。

## 所有权

| NLP服务 | Gateway | 工具/设备团队 |
| --- | --- | --- |
| Prompt、固定工具、模型策略、语义与结构校验、规划、推理预算、编排、表达 | 身份鉴权、会话历史/上下文、设备关联、checkpoint存储、唯一推进者、任务账本/租约、外部工具转交 | 资源查询、设备保护、真实执行、执行回执及未知结果核对 |

Gateway提供原话与事实，不提交system prompt、工具schema或模型参数。NLP不创建生产会话数据库、Redis、鉴权或设备连接。三个委托工具由NLP内部消费，不能转成车端命令。

## HTTP入口

启动：`.venv/bin/python -m uvicorn backend.nlp_app:configured_app --factory --port 8001`。必填`NLP_GATEWAY_URL`指向下文账本/诊断协议；模型与检索服务仍使用`config/services.toml`。

| 方法/路径 | 请求 | 响应 |
| --- | --- | --- |
| GET `/health/live` | 无 | 本进程ready及release/model/semantic policy版本；不表示外部服务健康 |
| POST `/v1/runs/start` | `StartRequest` | NDJSON事件流 |
| POST `/v1/runs/resume` | `ResumeRequest` | NDJSON事件流 |
| POST `/v1/runs/cancel` | `{identity}` | 对本实例活动请求返回`{accepted:true}`，不在本实例返回404 |
| POST `/v1/tasks/execute` | `TaskExecutionRequest` | `TaskExecutionResult`，执行到终态/澄清/修订边界 |
| POST `/v1/tasks/mutate` | `TaskMutationRequest` | Gateway确认的最新任务快照 |
| POST `/v1/tasks/present` | `TaskPresentationRequest` | 按版本/状态核对后的播报与结果卡 |

请求验证失败422；身份/版本/协议冲突409；并发满429；Gateway不可用503；任务HTTP期限504。校验响应不回显完整输入、checkpoint或凭证。默认每进程32并发、8 MiB请求上限、120秒HTTP推进期限；环境配置见[NLP运行说明](NLP_HARNESS.md)。

响应已经开始时不能再变更HTTP状态码。传输异常使用独立一行`{protocol_version:"gateway-v1",type:"transport.error",identity,error_code,outcome_unknown:true}`结束；它不是Agent完成事件、不是新checkpoint、更不能授权执行。断线会取消本次模型/子调用。Gateway只能依据已经完整接收并原子保存的交接记录处理后续，不因断线自动重新start或执行动作。

接口面向受信任的内部Gateway，部署层负责访问隔离/服务身份；本项目不重复实现用户鉴权。NDJSON代理需关闭缓冲。正在流式执行的cancel须路由同一实例；已挂起run使用resume的cancel字段。跨实例排他和重试决策由Gateway管理。

## 输入与上下文版本

`StartRequest`主要字段：

| 字段 | 语义 |
| --- | --- |
| `identity` | 不变的`run_id/request_id/turn_id/context_revision` |
| `conversation_id` | Gateway会话标识，后台登记/管理必填 |
| `received_at` | 含时区偏移的本轮接收时间，恢复时不重取 |
| `user_text` | 1–4000字原话，不能空白 |
| `speaker_seat` | driver/front_passenger/rear_left/rear_right |
| `persona_id` | professional/warm/playful，仅影响表达 |
| `history` | 结构化已结束/中断历史，不能包含当前turn或system角色 |
| `context` | 版本化事实，revision必须等于identity.context_revision |
| `budget` | 累计模型数、工具数及模型等待时间上限 |

`context`包含整体revision/observed_at，独立版本的vehicle与capabilities，environment（IANA timezone、带来源location），cockpit领域状态，以及带来源的tasks/places/media。run/task/conversation等通用标识最大128字符，place_ref最大300字符，媒体content_id最大500字符；带时区时间遵循ISO8601。能力列表可包含前台Planner使用的非热车控，不因此扩大Talker固定26工具。

工具能力显式区分`operation=query/control`、`execution_location=cloud/device`、`action`和`content_kind`。部署binding与能力事实必须同时匹配；缺能力不静默改执行位置。媒体/导航状态需要非负version；其字段契约沿用`backend/cockpit/controller.py`的真实状态/回执校验。

历史原生工具组须闭合；最后未完组可标记`pending_tool_group`，编译时剔除。主动任务历史带`source_task`；打断播放带`interrupted_playback/heard_text`，不能把没听到的全文当已告知。POI必须保留provider、source_turn_id、presented_to_user；本轮刚搜到的POI不视为用户选择。媒体ID来源于真实目录/本轮已接受的目录回执，不能来自模型编造URL。

发生新的用户输入、说话位置或会影响决策的上下文变更时，终止旧决策，使用新身份与新context revision启动新run。resume不能替换原话、上下文或版本。迟到回执只用于审计/刷新下一轮事实，不能重贴新identity后注入新run。

## 输出、编排与回执

`GatewayOutput`携带原identity与递增sequence；诊断事件也占sequence，所以允许间隙。

| type | 意义 |
| --- | --- |
| `text.delta` | 增量表达，不能证明执行完成 |
| `run.suspended` | 完整checkpoint与本次新释放的requests |
| `run.completed` | NLP本次推理完成及checkpoint |
| `run.failed` | 失败终态、error_code及审计checkpoint |
| `run.cancelled` | 取消终态及审计checkpoint |

只有`run.suspended.requests`授权新工具交接。终态checkpoint中的pending是审计信息，不能据此组装执行命令。Gateway必须原子保存checkpoint与同次requests后才转交执行；同时只允许一个推进者。协议不提供跨实例exactly-once；稳定幂等键与外部执行端去重共同约束重放。

每条request保留`invocation_id/tool_call_id/name/arguments/idempotency_key`；`dispatch`包含操作/位置、领域、`execution_arguments`、`prepared_command`和`depends_on`。原始模型参数用于审计，归一化执行参数与命令准备单独表示，不能悄悄篡改模型历史。内容ID解析、真实播放URL及厂商命令编码由对应执行适配器完成。

NLP内部执行`delegate_vehicle_control/delegate_task/manage_task`：前台Planner产出并校验计划后，只释放真实设备动作；TaskService收到Gateway登记确认后才返回registered。不是把NLP调度请求交给外部重新实现。后台运行由独立任务执行接口推进。

前台计划逐项推进；复合媒体/导航按依赖等待实际回执后重新准备。同组独立工具可一起交接。前一步失败、未知或缺少必需真实车况时，不能按模拟expected继续。纯控制/登记成功采用回执完成路径，避免额外模型调用；查询与需要解释的失败按真实结果继续推理。

`ResumeRequest={protocol_version,identity,checkpoint,observations,cancel}`。`cancel=true`不能同时提交observations。每条observation包含原identity、invocation_id、status、JSON result：

- `completed`：已有确定执行结果；业务失败使用`result.ok=false`。控制成功须有真实执行确认`executed=true`，不能提交仅预检成功的`ok=true`。每个Planner写动作（含最后一步）还须带完整实际`vehicle_state`。媒体/导航控制回执的state.version或state_version须等于命令expected_state_version+1。
- `rejected`：确定未执行/被拒绝，不能伪装成功。
- `unknown`：结果不确定；等待最终真实结果，不重发动作、不让模型猜测。

允许部分回执和乱序回执；一致重复可接受，已确定结果的冲突副本拒绝。unknown可被最终确定结果替换。等待状态不重复输出原requests，Gateway从自己已保存的交接记录恢复。完整原生tool消息按最初模型顺序回填。

取消只阻止后续推理/交接，不撤销已经发出的动作，不自动取消已登记的后台任务。取消后台任务需要任务mutation。失败/取消/完成run不可恢复；任务登记响应不确定时保留稳定task_id/idempotency_key供Gateway核对，不能自动新建另一个任务。

## Checkpoint与预算

checkpoint包含原StartRequest、内部运行状态、子计划/回执账本和三个版本：源码+prompt的`release_id`、实际模型、任务预算及检索提供商配置的`model_policy_id`、业务bindings的`semantic_policy_id`。生产组合根在启动时解析并冻结上述配置、计算版本，任一变化拒绝继续旧checkpoint；部署升级应保留旧版本完成/取消或启动新run，不自动迁移。

checkpoint只允许Gateway可信服务器存储回传，不能发给终端UI。内部包含完整上下文。一致性校验不等于数字签名，不能证明任意客户端构造的数据真实性。

模型/工具计数与模型等待预算跨resume累计，含前台子计划。HTTP期限独立；Gateway等待设备结果不消耗模型等待预算。后台任务有独立步骤、执行时间、quick查询和收尾预算；所有版本/租约仍需保持。

## Gateway任务账本HTTP接口

NLP以POST调用`NLP_GATEWAY_URL`，不自动重试写入。若设置`NLP_GATEWAY_TOKEN`，使用Bearer头；所有响应要求HTTP200及下面的严格JSON结构。409表示revision/lease冲突；5xx、超时、断线、非法响应代表结果可能未知。不要在错误正文中返回密钥。

共同路径：`/internal/nlp/conversations/{conversation_id}/tasks/{task_id}/{operation}`。路径ID编码，body共同带`conversation_id/execution_id/lease_token`；非执行登记/管理的后两项为null。Gateway须同时验证路径与body、会话所属、租约与当前revision，不能仅相信调用者的expected_revision。

| operation | body附加字段 | 响应 |
| --- | --- | --- |
| read | 无 | `{task: snapshot或null}` |
| refresh | 无 | `{task: snapshot或null}`，原子应用待处理更新并核对执行租约 |
| create | goal、context、idempotency_key | `{task: snapshot}`，稳定task_id，初始revision=1；重复相同请求返回同一注册记录 |
| mutate | expected_revision、action=reply/update/cancel、text、idempotency_key | `{task: snapshot}`；reply/update正文不能为空 |
| save | expected_revision、fields | `{applied: bool}`；状态、checkpoint、events原子提交，须有效租约 |
| expire | max_age_seconds | `{accepted:true}`；按已累积实际时长/澄清规则原子过期 |
| notify-progress | 无 | `{applied:bool}`；只生成符合阶段与去重规则的进度事件 |
| usage | 无 | `{execution_seconds:非负数,lookup_count:非负整数}` |

snapshot至少包括`id/session_id/goal/revision/status/deadline_at`。执行字段含`running_seconds/running_since/constraints/context/steps/checkpoint/sources/transcript`，保留JSON型业务字段。`session_id`必须等于conversation_id，id必须等于路径task_id；时间显式带时区。context保留登记时的release_id/model_policy_id及输入事实，Gateway不可自行替换推理版本。

Gateway拥有任务计时与状态规则：排队时间计入60秒上限，澄清暂停；回复后正确恢复剩余时间；同一revision只能有一个有效执行租约。reply/update/cancel成功提交均返回expected_revision+1；reply/update进入queued，cancel进入cancelling/cancelled并使旧执行写入失效；状态与事件同事务提交。执行中撤销/过期租约必须让refresh/save拒绝，不能只在execute入口检查一次。

`TaskExecutionRequest`要求conversation_id、task_id、revision、execution_id、lease_token、release_id、model_policy_id。NLP重新读取真实任务后执行；返回requested_revision、stale及最新任务快照。Gateway应先取得有效租约，断线后先核对任务和租约再恢复，不能并发启动同一revision的两个执行者。

`TaskMutationRequest`要求conversation_id、task_id、expected_revision、action、text、idempotency_key。外部用户权限由Gateway判断，NLP核对任务和版本后写入。所有成功响应代表实际账本已提交，不能先ACK再异步保存。

`TaskPresentationRequest={conversation_id,task_id,revision,expected_status,persona_id}`用于后台回流。NLP读取真实任务后核对版本/状态；不匹配返回stale且无speech/card，匹配则返回稳定event_key、kind、speech与card。表达复用既有话术与人设，不额外调用模型。Gateway以event_key去重，并在播放前再次核对当前任务/对话版本。建议动作仅作参考，要执行须在新的用户授权run中重新校验。

## 诊断

普通运行诊断POST `/internal/nlp/diagnostics`，body为`{records:[DiagnosticRecord,...],idempotency_key}`。idempotency_key为脱敏后整批内容的稳定哈希，Gateway原子持久化整批后返回`{accepted:true}`，重复批次不能重复记账。增量文字在单次运行内临时批量缓冲，模型完成、挂起/终态和流关闭前提交，避免每个token同步一次HTTP。任务诊断POST `/internal/nlp/conversations/{conversation_id}/traces`。诊断包括模型输入输出/元信息、工具交接、回执、任务阶段；不得向终端展示原始内容。

任务trace body为共同scope字段加`record={kind,data,...}`，响应`{accepted:true}`。usage需在相关trace确认后可见，防止恢复时重置quick查询预算。Gateway负责trace幂等、存储和读取权限。

诊断发送前按凭证字段、已知secret值及媒体能力URL脱敏；研究资料来源URL仍保留供核验。交接前诊断失败会导致`run.failed/diagnostics_unavailable`，不输出未审计的执行请求。诊断不可用也不能伪造正常完成。

## 联调顺序

1. 用模型替身与MockTransport运行本仓库合同测试，确认完整请求/回执/任务字段。
2. Gateway实现可信checkpoint存储、完整交接、幂等和租约/CAS；在同一版本跑start→工具→resume及登记→执行→澄清→修订链路。
3. 接真实工具/设备并覆盖断线、重复/迟到回执、未知结果、取消、能力变化及滚动升级。
4. 模型权限恢复后跑真实行为评测与延迟报告，再做实际座舱验证。离线替身通过不代表真实执行验收通过。

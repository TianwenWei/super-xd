# NLP / Gateway V1契约

这是本项目实现的目标集成契约，尚未经外部Gateway团队确认。以下路径均相对于 `src/super_xd/nlp/`：wire 模型位于 `contracts/gateway_v1.py`，内部运行数据在 `contracts/run.py`，策略/诊断端口在 `orchestration/ports.py`，后台请求在 `contracts/tasks.py`，任务账本端口与 HTTP 适配分别在 `orchestration/tasks/ports.py` 和 `adapters/gateway/tasks.py`；HTTP 入口位于 `api/app.py`。`gateway-v1` 是业务协议版本。未知字段、错误类型、非有限数值、无时区时间戳均拒绝。

## 导出可对接的Schema

```sh
.venv/bin/python -m scripts.export_gateway_contract --out data/contracts/gateway-v1
```

脚本直接从生产Pydantic模型导出14份独立Draft2020-12 JSON Schema（运行接口、任务快照/交付请求及通知仲裁数据），并逐份校验；不加载配置、凭证或供应商实例。生成物保存在忽略的data目录，不提交Git，可提供给Gateway团队生成客户端/执行契约测试。业务约束与错误/恢复语义仍以本文和实际实现为准。

## 所有权

| NLP服务 | Gateway | 工具/设备团队 |
| --- | --- | --- |
| Prompt、固定工具、模型策略、语义与结构校验、规划、推理预算、编排、表达 | 身份鉴权、会话历史/上下文、设备关联、checkpoint存储、唯一推进者、任务账本/租约、外部工具转交 | 资源查询、设备保护、真实执行、执行回执及未知结果核对 |

Gateway提供原话与事实，不提交system prompt、工具schema或模型参数。NLP不创建生产会话数据库、Redis、鉴权或设备连接。三个委托工具由NLP内部消费，不能转成车端命令。

## HTTP入口

启动：`.venv/bin/python -m uvicorn super_xd.nlp.api.app:configured_app --factory --port 8001`。必填`NLP_GATEWAY_URL`指向下文账本/诊断协议；模型与检索服务仍使用`config/services.toml`。

| 方法/路径 | 请求 | 响应 |
| --- | --- | --- |
| GET `/health/live` | 无 | `{status:"alive"}`，仅进程存活 |
| GET `/health/ready` | 无 | 本地资源/可选Gateway健康探测及release/model/semantic policy版本；未就绪503 |
| GET `/metrics` | 无 | 按模型角色聚合的调用、失败/取消、时长和已报告token用量 |
| POST `/v1/runs/start` | `StartRequest` | NDJSON事件流 |
| POST `/v1/runs/resume` | `ResumeRequest` | NDJSON事件流 |
| POST `/v1/runs/cancel` | `{identity}` | 对本实例活动请求返回`{accepted:true}`，不在本实例返回404 |
| POST `/v1/tasks/execute` | `TaskExecutionRequest` | `TaskExecutionResult`，执行到终态/澄清/修订边界 |
| POST `/v1/tasks/mutate` | `TaskMutationRequest` | Gateway确认的最新任务快照 |
| POST `/v1/tasks/present` | `TaskPresentationRequest` | 按版本/状态核对后的播报与结果卡 |

请求验证失败422；身份/版本/协议冲突409；并发满429；Gateway不可用503；任务HTTP期限504。校验响应不回显完整输入、checkpoint或凭证。默认每进程32并发、8 MiB HTTP body上限、30秒body接收期限、120秒HTTP推进期限；环境配置见[NLP运行说明](NLP_HARNESS.md)。

resume的交接归属、已确认回执冲突及同批回执冲突在流启动前校验，返回409/run_protocol_conflict，整批回执不被采纳。相同回执重复提交仍保持幂等；unknown可由后续确认结果解决，已确认结果不能被改写。

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

`context`包含整体revision/observed_at，独立版本的vehicle与capabilities，environment（IANA timezone、带来源location），cockpit领域状态，以及带来源的tasks/places/media。run/task/conversation等通用标识最大128字符，place_ref最大300字符，媒体content_id最大500字符；带时区时间遵循ISO8601。能力列表最多128行，可包含前台Planner使用的非热车控，不因此扩大Talker固定26工具。NLP给Talker的模型副本只展开常驻工具的声明，冷车控按skill列出available/unavailable/missing名称及可用数；完整StartRequest声明仍用于前台动作校验，压缩不授予新能力。

工具能力显式区分`operation=query/control`、`execution_location=cloud/device`、`action`和`content_kind`。部署binding与能力事实必须同时匹配；缺能力不静默改执行位置。媒体/导航状态需要非负version；其字段契约沿用`src/super_xd/nlp/policy/cockpit.py`的真实状态/回执校验。

历史原生工具组须闭合；最后未完组可标记`pending_tool_group`，编译时剔除。主动任务历史带`source_task`。assistant消息保留完整生成内容；`playback_status=unknown/not_started/partial/completed`与`heard_text`独立描述实际播放，默认unknown，not_started不能带非空heard_text。打断另带`interrupted_playback`。模型可用完整生成内容理解追问，但不能把未播出的全文当作已听；未知反馈也不能将答案替换为空。POI必须保留provider、source_turn_id、presented_to_user；本轮刚搜到的POI不视为用户选择。媒体ID来源于真实目录/本轮已接受的目录回执，不能来自模型编造URL。

历史工具调用可带`extra_content`对象（单个最多64 KiB）；assistant记录另可带`provider_metadata={"extra_content": {...}}`（整个provider_metadata最多64 KiB，仅允许extra_content对象），用于保存供应商顶层续推数据。两者原样保存模型返回的续推元数据，例如Gemini的`google.thought_signature`。Gateway从可信模型transcript持久化并回传，不能生成、拼接、截断或改写签名；该字段不授予工具权限，也不送入设备执行参数。同一run的checkpoint及后续模型请求同样保留原值；更换模型策略必须启动新run。

发生新的用户输入、说话位置或会影响决策的上下文变更时，终止旧决策，使用新身份与新context revision启动新run。resume不能替换原话、上下文或版本。迟到回执只用于审计/刷新下一轮事实，不能重贴新identity后注入新run。

## 输出、编排与回执

`GatewayOutput`携带原identity与递增sequence；诊断事件也占sequence，所以允许间隙。终态可带`presentation={speech,source}`，与checkpoint中的presentation必须一致；Gateway按identity与sequence去重并原样展示/送TTS。该表达由NLP根据已接受的真实回执和冻结人设生成，不需要Gateway再猜成功或补话术，也不增加模型调用。已有模型预告/最终答案避免重复；未知或取消不生成完成播报。原始模型transcript仍独立保留，闭合工具组的展示话术进入对话历史。

| type | 意义 |
| --- | --- |
| `text.delta` | 增量表达，不能证明执行完成 |
| `run.suspended` | 完整checkpoint与本次新释放的requests |
| `run.completed` | NLP本次推理完成及checkpoint |
| `run.failed` | 失败终态、error_code及审计checkpoint |
| `run.cancelled` | 取消终态及审计checkpoint |

只有`run.suspended.requests`授权新工具交接。终态checkpoint中的pending是审计信息，不能据此组装执行命令。Gateway必须原子保存checkpoint与同次requests后才转交执行；同时只允许一个推进者。协议不提供跨实例exactly-once；稳定幂等键与外部执行端去重共同约束重放。

每条request保留`invocation_id/tool_call_id/name/arguments/idempotency_key`；`dispatch`包含操作/位置、领域、`execution_arguments`、`prepared_command`和`depends_on`。原始模型参数用于审计，归一化执行参数与命令准备单独表示，不能悄悄篡改模型历史。内容ID解析、真实播放URL及厂商命令编码由对应执行适配器完成。

NLP内部执行`delegate_vehicle_control/delegate_task/manage_task`：前台Planner产出并校验计划后，只释放真实设备动作；TaskService收到Gateway登记确认后才返回registered。登记ACK确认后TaskCommit保持confirmed，后续前台等待/交付失败以`task_delivery_unconfirmed`单独报告，不能改成unknown登记或自动新建任务。不是把NLP调度请求交给外部重新实现。后台运行由独立任务执行接口推进。

前台计划逐项推进；复合媒体/导航按依赖等待实际回执后重新准备。同组独立工具可一起交接。前一步失败、未知或缺少必需真实车况时，不能按模拟expected继续。纯控制/登记成功采用回执完成路径，避免额外模型调用；查询与需要解释的失败按真实结果继续推理。

`ResumeRequest={protocol_version,identity,checkpoint,observations,cancel}`。`cancel=true`不能同时提交observations。每条observation包含原identity、invocation_id、status、JSON result：

- `completed`：已有确定执行结果；业务失败使用`result.ok=false`。控制成功须有真实执行确认`executed=true`，不能提交仅预检成功的`ok=true`。热车控及每个Planner写动作（含最后一步）还须带完整实际`vehicle_state`；缺完整事实不能判为确定成功。媒体/导航控制回执的state.version或state_version须等于命令expected_state_version+1。
- `rejected`：确定未执行/被拒绝，不能伪装成功。
- `unknown`：结果不确定；等待最终真实结果，不重发动作、不让模型猜测。

允许部分回执和乱序回执；一致重复可接受，已确定结果的冲突副本拒绝。unknown可被最终确定结果替换。等待状态不重复输出原requests，Gateway从自己已保存的交接记录恢复。完整原生tool消息按最初模型顺序回填。

取消只阻止后续推理/交接，不撤销已经发出的动作，不自动取消已登记的后台任务。取消后台任务需要任务mutation。失败/取消/完成run不可恢复；任务登记响应不确定时保留稳定task_id/idempotency_key供Gateway核对，不能自动新建另一个任务。

## Checkpoint与预算

业务协议仍为`gateway-v1`，运行wire checkpoint为`run.checkpoint_version=2`。它包含原StartRequest、本轮新生成消息（不重复固定prompt/schema/history前缀）、预算/sequence、子计划与回执账本。`prepared_digest`绑定完整编译结果；恢复时用原StartRequest与同版本编译器重建前缀并校验摘要，改动原话、事实或工具定义都会拒绝。子编排车况按领域保存相对初始车况的差量。旧checkpoint_version=1不自动迁移。

checkpoint另绑定三个版本：源码+prompt的`release_id`、实际模型、任务预算、前台软等待期限及检索提供商配置的`model_policy_id`、业务bindings的`semantic_policy_id`。生产组合根在启动时解析并冻结上述配置、计算版本，任一变化拒绝继续旧checkpoint；部署升级应保留旧版本完成/取消或启动新run，不自动迁移。

checkpoint只允许Gateway可信服务器存储回传，不能发给终端UI。内部包含完整上下文。一致性校验不等于数字签名，不能证明任意客户端构造的数据真实性。

大小按紧凑JSON的UTF-8字节计：StartRequest≤6 MiB，checkpoint除start以外≤1 MiB，单批observations≤256 KiB。超限类型校验返回422及start_size_exceeded/checkpoint_size_exceeded/observations_size_exceeded。resume还在接收任何回执前预留其在账本/消息中的空间；不足返回409/checkpoint_size_exceeded，此批回执未被接收。已开始的流若无法构造下一checkpoint，返回transport.error/checkpoint_size_exceeded，不能当作新执行授权。Gateway保留此前已确认交接与真实结果；不因超限重新执行动作。完整provider请求、schema和诊断由独立trace保存，不能静默截断业务事实。HTTP body仍受独立8 MiB默认上限约束。

模型/工具计数与模型等待预算跨resume累计，含前台子计划。HTTP期限独立；Gateway等待设备结果不消耗模型等待预算。后台任务有独立步骤、执行时间、quick查询和收尾预算；所有版本/租约仍需保持。

## 证据与检索收束

NLP在模型结果副本中附加evidence_scope、evidence_refs和retrieval_progress，不改写原始回执。引用由来源ID、版本及内容/范围指纹构成；分数、排序和再次查询时间不算新增证据。范围只说明材料覆盖和粒度，answer_support_assessed=false明确表示未替模型判断答案是否被支持。

前台OrchestrationCheckpoint.retrieval按已接受、确定的检索回执累计，恢复时与原回执账本核对；unknown不计作确定结果。首次空结果允许一次重查，后续无新增证据阻止继续调用同一工具并返回retrieval_progress_exhausted，提示使用已有材料回答。默认每工具最多4次、全部检索最多8次；后台TaskRunner在自己的任务checkpoint保存同一进展模型，并受任务预算共同限制。没有额外自然语言分类器，也不以引用数量证明事实覆盖完整。

## Gateway任务账本HTTP接口

NLP以POST调用`NLP_GATEWAY_URL`，不自动重试写入。若设置`NLP_GATEWAY_TOKEN`，使用Bearer头；所有响应要求HTTP200及下面的严格JSON结构。409表示revision/lease冲突；5xx、超时、断线、非法响应代表结果可能未知。不要在错误正文中返回密钥。

共同路径：`/internal/nlp/conversations/{conversation_id}/tasks/{task_id}/{operation}`。路径ID编码，body共同带`conversation_id/execution_id/lease_token`；非执行登记/管理的后两项为null。Gateway须同时验证路径与body、会话所属、租约与当前revision，不能仅相信调用者的expected_revision。

| operation | body附加字段 | 响应 |
| --- | --- | --- |
| read | 无 | `{task: snapshot或null}` |
| refresh | 无 | `{task: snapshot或null}`，原子应用待处理更新并核对执行租约 |
| create | goal、context、delivery_mode=foreground/background、idempotency_key | `{task: snapshot}`，稳定task_id，初始revision=1；重复相同请求返回同一注册记录 |
| delivery-mode | expected_revision、expected_delivery_mode=foreground、delivery_mode=background、idempotency_key | `{task: snapshot}`；仅变更交付模式，原revision及执行租约保持 |
| mutate | expected_revision、action=reply/update/cancel、text、idempotency_key | `{task: snapshot}`；reply/update正文不能为空 |
| save | expected_revision、fields | `{applied: bool}`；状态、checkpoint、events原子提交，须有效租约 |
| expire | max_age_seconds | `{accepted:true}`；按已累积实际时长/澄清规则原子过期 |
| notify-progress | 无 | `{applied:bool}`；只生成符合阶段与去重规则的进度事件 |
| usage | 无 | `{execution_seconds:非负数,lookup_count:非负整数}` |

snapshot至少包括`id/session_id/goal/revision/status/deadline_at`，另有`delivery_mode=foreground/background`（旧快照未提供时按background读取）。执行字段含`running_seconds/running_since/constraints/context/steps/checkpoint/sources/transcript`，保留JSON型业务字段。`session_id`必须等于conversation_id，id必须等于路径task_id；时间显式带时区。context保留登记时的release_id/model_policy_id及输入事实，Gateway不可自行替换推理版本。

Gateway拥有任务计时与状态规则：排队时间计入60秒上限，澄清暂停；回复后正确恢复剩余时间；同一revision只能有一个有效执行租约。reply/update/cancel成功提交均返回expected_revision+1；reply/update进入queued，cancel进入cancelling/cancelled并使旧执行写入失效；状态与事件同事务提交。执行中撤销/过期租约必须让refresh/save拒绝，不能只在execute入口检查一次。

`delivery-mode`在非执行scope下调用，execution_id/lease_token为null，不是获取或撤销执行租约的操作。Gateway验证会话所属、expected_revision和预期交付模式，原子修改delivery_mode、记录delivery_changed事件及幂等结果；与checkpoint保存串行化，但不得清空/回退当前checkpoint。状态、revision、执行者、租约、steps、累计运行时间与证据均保持。相同幂等键/请求返回同一结果；不同请求复用键应冲突。已经background可幂等返回；已取消/正在取消保持取消状态。失败或响应未知不允许自动换键重试。

`TaskDeliveryRequest={conversation_id,task_id,expected_revision,idempotency_key}`是NLP内部交付方法的数据契约，对应上述Gateway账本操作，不新增`/v1/tasks` HTTP路由。前台等待默认8秒，等待到期或断线时尝试解除前台交付；已持租约的原执行者继续任务。Gateway仍需独立调度`execute`，本项目不通过该操作实施跨worker租约迁移。completed与切换可能竞争，Gateway须保留实际完成快照，并保证结果通过前台或后续事件获得交付。

`TaskExecutionRequest`要求conversation_id、task_id、revision、execution_id、lease_token、release_id、model_policy_id。NLP重新读取真实任务后执行；返回requested_revision、stale及最新任务快照。Gateway应先取得有效租约，断线后先核对任务和租约再恢复，不能并发启动同一revision的两个执行者。

`TaskMutationRequest`要求conversation_id、task_id、expected_revision、action、text、idempotency_key。外部用户权限由Gateway判断，NLP核对任务和版本后写入。所有成功响应代表实际账本已提交，不能先ACK再异步保存。

`TaskPresentationRequest={conversation_id,task_id,revision,expected_status,persona_id}`用于后台回流。NLP读取真实任务后核对版本/状态；不匹配返回stale且无speech/card，匹配则返回稳定event_key、kind、speech与card。前后台表达共用task_presentation，按context.read_details选口语摘要或格式清理后的全文，再应用表达层人设，不额外调用模型。Gateway以event_key去重，并在播放前再次核对当前任务/对话版本。建议动作仅作参考，要执行须在新的用户授权run中重新校验。

## 通知仲裁

导出的`TaskNotice`包含event_id/task_id/revision/status/kind/priority/created_at/expires_at；时间必须显式带时区。`SpeakingContext`包含connected、user_speaking、assistant_speaking、generating、in_call、demanding_driving事实。`PresentationDecision`为`speak/defer/screen_only/drop`及reason；这是纯策略数据契约，当前没有独立HTTP仲裁路由。

生产Gateway持有队列、真实注意力事实、去重和交付状态。过期/旧版本通知丢弃，普通progress只更新界面；对话忙、断线、通话和繁忙驾驶时延后，澄清优先。Demo提供同规则的actor队列，普通结果有效期10分钟、澄清使用clarification_expires_at（执行deadline_at在澄清时暂停），重复事件不延长有效期。展示数据读取失败按0.25秒至15秒退避且不超过通知有效期；已交付/过期/过时通知的迟到回调不能触发新播报。实际播放前仍须核对任务与对话版本；模型生成文字不证明用户已经听到。

## 诊断

普通运行诊断POST `/internal/nlp/diagnostics`，body为`{records:[DiagnosticRecord,...],idempotency_key}`。idempotency_key为脱敏后整批内容的稳定哈希，Gateway原子持久化整批后返回`{accepted:true}`，重复批次不能重复记账。增量文字在单次运行内临时批量缓冲，模型完成、挂起/终态和流关闭前提交，避免每个token同步一次HTTP。任务诊断POST `/internal/nlp/conversations/{conversation_id}/traces`。诊断包括模型输入输出/元信息、工具交接、回执、任务阶段；不得向终端展示原始内容。

车控规划的完整模型观察、技能加载资料和中间事件记录为`DiagnosticRecord(kind="orchestration.event")`，不属于`VehiclePlan`业务字段。计划、工具结果和checkpoint不得携带`model_observations/load_evidence/trace_events`诊断副本；预算计数、来源、校验与动作账本仍完整保存，业务结果不能因诊断体积而被截断。

任务trace body为共同scope字段加`record={kind,data,...}`，响应`{accepted:true}`。usage需在相关trace确认后可见，防止恢复时重置quick查询预算。Gateway负责trace幂等、存储和读取权限。

诊断发送前按凭证字段、已知secret值及媒体能力URL脱敏；研究资料来源URL仍保留供核验。交接前诊断失败会导致`run.failed/diagnostics_unavailable`，不输出未审计的执行请求。诊断不可用也不能伪造正常完成。

## 联调顺序

1. 用模型替身与MockTransport运行本仓库合同测试，确认完整请求/回执/任务字段。
2. Gateway实现可信checkpoint存储、完整交接、幂等和租约/CAS；在同一版本跑start→工具→resume及登记→执行→澄清→修订链路。
3. 接真实工具/设备并覆盖断线、重复/迟到回执、未知结果、取消、能力变化及滚动升级。
4. 按目标部署配置跑重复真实行为评测与延迟报告，再做实际座舱验证。离线替身通过不代表真实执行验收通过。

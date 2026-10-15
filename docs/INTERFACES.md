# 原型接口

最新范围：不实现车控目录 embedding/语义检索；Thinker 的真实 Tavily 网页搜索保留。Talker固定15个热车控、3个调度和3个地点/天气schema。其余85个车控函数只经Pro车控规划层渐进加载，始终不追加到Talker的可调用工具集合。

## 渐进式车控协议（2026-10-14）

Talker调度函数`delegate_vehicle_control({skill_ids:[...]})`只接受子系统ID；actor提供未经改写的本轮原句、完整车况、近期历史与本轮真实已完成operations。涉及冷热混合的请求整体委托；同组含委托和直接车控、重复委托或直接冷函数时，整组在任何动作下发前拒绝，由同一Mini在原步骤上限内重选。

独立`VehicleControlPlanner`调用`VEHICLE_CONTROL_MODEL`（默认Pro，thinking disabled），只开放`load_skill`和`submit_vehicle_plan`。`submit_vehicle_plan`参数包含`status/intents/actions/keep_paths/question/reason`：intent绑定原文准确span、action IDs或真实completed operation IDs；action包含`id/intent_id/name/arguments/depends_on`，参数使用实际函数schema；keep_paths为真实状态点路径。能力仅在该委托的Pro上下文扩张，计划不执行车辆。

模型仅提交逐字`source_text`；程序仅在原句中有唯一精确匹配时补内部`source_start/end`，无匹配或重复片段均拒绝，不猜位置或修改原文。模型侧省去两份完整保持路径枚举，服务端仍按实际快照路径严格验证。`planned`意图的保持路径为空，显式保持条件使用独立`keep`意图；顶层路径必须等于这些意图的并集，不自动修删模型错误字段。

actor以最新车况对整组计划预检，通过后按依赖和读写冲突排序，独立动作并行下发；实际commit回执后才把intent核销为completed。失败动作、阻塞依赖、保持项与打断分别记录。外层委托tool_result包含`source:vehicle_plan`、`status`、`intents`、`action_receipts`、`keep_paths`、`read_results`，只把真实结果回填Talker；Pro的内部调用不伪造为Mini自己的tool_calls。原文未覆盖字符只记录warning，不触发重试；结构及来源绑定验证不证明任意自然语言语义正确。

模拟车端持有独立状态，实际执行前重新核对接受计划时的保持基线、最新写集合和执行副本的最终差异，通过才提交。依赖动作必须收到前置成功回执才下发。迟到旧回执仅记trace；结束时保持项按实际值核销。

下发动作时立刻播报进行中反馈，`vehicle.dispatch_feedback`关联command_id，`completion_claimed:false`。成功回执不再次朗读，失败用真实reason补报；超时报告状态未确认。界面最终完成文字仍来自回执。

新增轨迹：`vehicle.plan.started/received/accepted/rejected/completed`区分规划、预检和真实执行；`vehicle.plan.action_started/action_completed`关联各项。计划生命周期`source:vehicle_planner`，实际动作`source:vehicle_plan`。`vehicle.command`与`vehicle.dispatched/receipt`附加`parent_call_id/plan_call_id/action_id/intent_id`；`session.snapshot.turns[].vehicle_plans`保留`parent_call_id/plan/ledger/action_receipts`。完整原始模型观察和加载证据保存在轨迹中，规划接受与simulation均`executed:false`，ACK不计成功。

前端将`vehicle_plans[].plan.actions`投影为独立的“Pro规划”视图，与原生`vehicle_calls`及实时事件按`parent_call_id:action_id`去重合并，重连可恢复。没有匹配的真实action receipt时显示待回执，不把simulation或整组plan.ok当作执行；该视图不会插入Mini模型历史。

`GET /api/config`的`model.vehicle_planner`报告配置的规划模型，`vehicle_control`报告`mode:progressive_pro`、`hot_count:15`与`delegated_count:85`。研究任务`delegate_task`和研究建议确认流程独立，不用于直接车控委托。

公共 Pydantic 类型在 `backend/types.py`：`ToolSpec`、`SkillManifest`、`PersonaConfig`、`SessionSnapshot`、`TaskEvent`。前端对应 TypeScript 类型在 `frontend/src/types.ts`。

## HTTP

- `GET /api/health`：模型连接预热与服务健康；检索明确 disabled。
- `GET /api/config`：persona、100函数/10skill/热工具、模型、TTS及ASR配置状态。无密钥。
- `POST /api/sessions`：创建 session_id。
- `POST /api/sessions/{session_id}/vehicle/commands/{command_id}/execute`：执行已登记模拟命令；按command_id幂等。默认200ms下行、300ms执行、200ms上行；返回receipt_id、ok、reason_code、resume_condition、state、state_version、timings。未知命令拒绝，浏览器只能把该回执ID回传。
- `GET /api/sessions/{session_id}`：车况、任务、近期审计 `history`（最近 12 轮），以及 `turns` 轮次汇总（turn_id/text/output/finished/proactive/task_id/persona_version/metrics）。完整持久化记录通过 traces 接口读取。
- `GET /api/sessions/{session_id}/traces`：模型实际输出与 Agent 轨迹，支持增量分页和 turn/task 筛选，详见下节。
- `POST /api/sessions/{session_id}/tts`：保留兼容的完整 MP3 接口，`{turn_id,text}`；仅当前已完成轮、文本需匹配该轮输出。新界面主流程使用下述独立 PCM WebSocket。新轮取消合成并拒绝迟到结果，缺 Speech 凭证明确报错。

## 轨迹读取

`GET /api/sessions/{session_id}/traces?after_id=0&limit=200` 返回：

```json
{
  "events": [],
  "next_after_id": 0,
  "has_more": false,
  "recording_error": null
}
```

`after_id` 为非负 trace_id，默认 0；`limit` 默认及上限 200，最小 1。可附加 `turn_id`、`task_id`，两个过滤条件与 session 条件同时成立，不能读取其他会话记录。返回事件按 trace_id 严格升序；下一页使用 `next_after_id`，直到 `has_more=false`，之后仍可用该游标轮询新增事件。空页保留传入游标；不同会话和后台并发写入可能使 ID 不连续。无效游标/页大小返回 422，会话不存在返回 404。

每条事件字段：

| 字段 | 含义 |
|---|---|
| trace_id / session_id | SQLite 全局递增记录 ID / 所属会话 |
| turn_id / task_id | 原始轮次 / 任务关联，可为 null；worker 记录可携带模型委托时的原轮次关联，手工任务不继承上一轮 |
| revision / step | 产生该记录的任务版本 / 推理步骤，可为 null |
| call_id | 本次模型调用 ID，或供应商 tool_call ID；按事件种类区分，无关联调用时为 null |
| kind / status | 事件类型与实际状态；例如 completed、failed、cancelled、stale |
| created_at | 观察到事件时的 UTC ISO 时间；分页顺序仍以 trace_id 为准 |
| data | 完整事件数据，含 agent、observed_at 及各事件实际字段 |

主要事件及数据：

- `model.started`：实际发送的 messages/tools；Talker 带 model_call_index，Thinker 带 request、revision/step。每次模型调用生成独立 call_id，load_skill 后再次调用不会复用前次 ID。
- `model.completed`：完整 message 与 meta。message 保留 content、tool_calls、实际返回的 reasoning_content 及扩展字段；meta 保留供应商 ID、model、created、finish_reason、usage、接收时间与耗时。Talker meta.raw_chunks 保存收到的 SSE JSON 帧，Thinker meta.raw_response 保存完整非流式 JSON 响应。
- `model.cancelled` / `model.error`：取消或错误及实际收到的 partial message/meta；未收到的数据不补造。旧轮次的已返回响应和错误仍保留，并标 stale/cancelled，不能触发旧工具或覆盖新回答。
- `tool.started` / `tool.completed` / `tool.error`：工具名称、原始或解析后的参数、完整结果/拒绝。data.model_call_id 关联产生该工具调用的模型调用，call_id 关联 tool_call；两个 ID 用于区分同一轮的多次调用。
- 字面校验拒绝时，`tool.error.data.result` 包含 `authorization_guard`（kind、原句 evidence span、适用的属性/当前值）、`executed:false`；没有对应 `vehicle.dispatched`。`literal_keep_state`覆盖氛围灯电源保持；`literal_target_mismatch`覆盖五类舒适功能和两类灯光的明确目标混用；`literal_window_hold`覆盖保留车窗与位置写集合冲突；`missing_required_origin`覆盖缺出发地的字面最近机场导航。后三类带`retry_model:true`，同一Talker可继续；未覆盖的文本仍须模型理解，None不表示完整授权证明。空会话误用 `manage_task` 返回 `code:no_task,retry_model:true,executed:false`。原始错误尝试不当作成功动作回执。
- `vehicle.dispatched` / `vehicle.receipt`：命令 ID、参数、实际模拟结果、耗时或超时；旧轮次的迟到回执标 stale，仍保存真实执行结果。ACK 不是成功回执，重复 ACK 不产生第二次执行或回执记录。
- `turn.started` / `turn.interrupted` / `turn.completed`：输入、打断或最终输出与 metrics。turn.completed 的 output_source 区分 model_content 与 persona_template；工具成功提示、拒绝和后台回流模板不会冒充模型原文。
- `task.*`、`revision.*`、`step.*`：任务生命周期、条件版本变化及 Thinker 执行步骤；记录旧版本的观察结果不表示该版本结果已被采纳。

content=null 表示供应商的空内容/仅工具调用；usage=null 或字段缺失表示未返回，不能解读为 0 token。reasoning_content 只展示供应商实际返回的内容；未返回时显示未提供，不推测、补写或宣称访问内部推理。原始返回内容不以摘要替代，脱敏除外；轨迹功能启用前未采集的供应商元字段无法补回。

Ark流请求使用stream_options.include_usage=true，但usage仍以实际返回为准。meta.response_headers_ms从本次HTTP请求开始至收到headers，含连接池、建连与HTTP等待，不是纯prefill；first_tool_delta_ms是同请求首个tool_calls delta时间，tool-only时first_delta_ms可为null；service_tier只记录供应商值。正常SSE遇[DONE]后读至HTTP EOF供连接池复用，取消仍可关闭流。本地真实TCP回归已验证同一连接的顺序复用，不能据此补造旧线上请求的复用或缓存命中证据。

Talker 用独立队列和线程异步写入，SQLite 日志操作不阻塞前台事件循环；Thinker 在独立 worker 中以短事务写入。读取可能短暂落后于流式文字或回执。`recording_error` 非 null 表示曾发生写入失败，界面明确提示记录可能不完整，不能把缺失事件当作没有调用。

记录位于本机 SQLite（默认 `data/super_xd.sqlite3` 的 trace_events），持久化时递归脱敏凭证字段、已知环境密钥值与 Bearer 文本，不记录客户端认证请求头。用户对话、系统上下文、模型输出和工具结果仍为本机明文调试数据；未提供日志加密或生产级鉴权。数据库持久化不改变当前只验收连续在线会话、未实现完整服务重启恢复的范围。

前端顶部、助手消息及任务卡提供“模型输出 / Agent 轨迹”入口；抽屉支持全会话/轮次/任务范围、模型原文/完整轨迹、关联调用筛选、完整事件 JSON 和复制。展开时按游标拉取全部分页并轮询新增记录，关闭保留缓存，切换会话清空缓存；轨迹读取失败不清除已读记录。

本轮真实轨迹与修复前证据见[轨迹验收](TRACE_VALIDATION.md)。旧模型回调是否 stale 由 actor 消费顺序决定，网络接收时间单独保留；已完成的旧模型输出仍可回看，不会因新输入先进入队列而误标为当前已采纳结果。

## WebSocket `/ws/{session_id}`

每条消息为 `{type:string,...}` JSON。前端 Vite 5173 代理本机 API 8000。

客户端：

| type | 字段 |
|---|---|
| input | text（1–4000字），client_request_id 可选 |
| persona.set | preset：professional/warm/playful，或 config：PersonaConfig |
| playback | speaking：boolean、turn_id、playback_id；当前 PCM 播放必须匹配两个 ID，合成等待/播放中 true，Worklet 排空/停止/error false |
| audio.configure | enabled：boolean；开启时可预热真实 Speech 连接，不合成占位或缓存声音 |
| asr.capture | capture_id、active：boolean；录音占位与释放，期间暂停助手音频及后台主动播报；过期 ID 的释放无效 |
| audio.cancel | turn_id、playback_id 可选；取消对应轮次的服务端合成，前端同时清空 Worklet；错误 playback_id 不取消当前播放 |
| audio.client_metrics | turn_id、playback_id、metrics；仅匹配的播放归入对应轮次，供应商与浏览器指标分开 |
| vehicle.configure | settings：speed_kph、gear、navigation_active、call_active、timezone（IANA）；P档不能有车速 |
| vehicle.receipt | command_id、receipt_id；必须匹配模拟执行端真实保存的回执，拒绝自报成功与旧ACK |
| audio.played | turn_id、playback_id、played_ms；AudioWorklet真实渲染样本数换算，支持已打断轮次归档 |
| task.create | goal |
| task.get | task_id 可选；多个任务不明确则返回消歧 |
| task.reply / task.update | task_id、text、expected_revision 可选 |
| task.cancel | task_id、expected_revision 可选 |

服务端：

| type | 字段 |
|---|---|
| session.snapshot | session_id、persona、persona_version、vehicle、tasks、history、turns；每轮 vehicle_calls 保留生成的车控调用原文 |
| turn.started | turn_id、text、persona_version、client_request_id，后台回流带 proactive/task_id |
| stream.delta | turn_id、text |
| stream.done | turn_id、text、metrics（首有效字/总延迟/路由/模型调用数/命令回执） |
| stream.interrupted | turn_id |
| vehicle.function_call | turn_id、call（id、type、function.name、function.arguments）；模型生成车控调用后、参数校验前展示，不表示执行成功 |
| vehicle.command | command_id、turn_id、name、arguments |
| vehicle.result | command_id、turn_id、result、latency_ms；**实际状态校验/提交后** |
| vehicle.state | state |
| persona.changed | persona、persona_version |
| task.event / ui.card | task，事件另带 event_id/revision |
| error | message、code/candidates 可选 |
| audio.connection | Speech 连接预热/复用状态与诊断耗时，无凭证或音频内容 |
| lookup.result | turn_id、call_id、tool、result；前台 POI/天气实际查询结果，按 call_id 合并 |

### 地点与天气 FC

`GET /api/config` 的 `maps` 字段仅返回配置是否存在、提供方及模拟导航范围。每轮 `session.snapshot.turns[].lookup_results` 保留 `{call_id,tool,result}`，便于重连还原卡片。完整结果进入 `tool.completed` 轨迹；无后台任务与 Thinker 轮询。

| FC | 参数 | 结果及行为 |
|---|---|---|
| search_places | query：含城市/区域的文本；max_results：1–5，默认5 | Google Places (New) 返回 places，每项含 place_id/name/address/location/country_code/maps_url/attributions；不修改导航 |
| get_weather | location：城市及国家；date：now/today/tomorrow/YYYY-MM-DD | Google Places 城市消歧后获取 Google Weather；结果含 provider/place/date/timezone/mode/summary/details/queried_at/source_url/notice |
| start_poi_navigation | place_id：本会话真实搜索结果 ID | 复用 set_navigation_destination 模拟命令/回执；成功后 navigation.destination_place 保存所选地点；无实际路线 |

日期按地点 IANA 时区计算，并匹配 Google `displayDate`（Google 日预报区间为当地07:00至次日07:00）。指定日期限定当地今天至未来第9天；请求日期无记录则返回错误，绝不拿别的日期替代。城市多解返回 `needs_clarification/candidates`，不默选第一项。天气返回404时才改用 Open-Meteo，显式标注 `provider=Open-Meteo`、`fallback_reason=google_unsupported_location`；权限/配额/超时均直接报错。原始天气字段保留在 details，summary 使用摄氏度，Open-Meteo WMO代码附中文释义。

查询协程不阻塞 actor 收件；整次 FC 最长25秒，HTTP单次超时10秒。用户打断取消在途查询，旧轮结果不进入候选注册表。注册表仅保留本会话最近50个真实地点；虚构/失效 ID 不下发导航。普通文字目的地更新和取消导航会清除旧 destination_place。所有 Google 密钥仅经服务端请求头发送，轨迹脱敏。

主服务读取环境或项目 `.env` 的 `GOOGLE_MAPS_API_KEY`，不执行 `.zshrc`。验收脚本可安全提取 `.zshrc` 中的字面量赋值。接口依据 [Places Text Search](https://developers.google.com/maps/documentation/places/web-service/text-search)、[Google Weather](https://developers.google.com/maps/documentation/weather)、[覆盖范围](https://developers.google.com/maps/documentation/weather/coverage)和 [Open-Meteo](https://open-meteo.com/en/docs)。

单次纯 POI 查询的最终口述列表由真实结果按原序生成，与卡片序号及 `recent_places` 一致；避免模型自行重排后“第一个”选错。原模型文字保留在 model.completed，最终来源标记为 `verified_place_results`。直接“导航到明确地点”在查到唯一候选后继续调用导航 FC，收到车端回执即结束，无额外润色模型。

任务状态：queued / running / waiting_clarification / cancelling / completed / failed / cancelled。task含id/goal/constraints/context/status/revision/summary/result/question/error/steps/sources/created_at/updated_at。context包含登记时车况、近期对话、活跃任务与时区，以及按revision保留的澄清问答。来源包含真实工具生成的 URL、queried_at、extracted 状态。

Talker调度工具为固定三个：`delegate_vehicle_control`、`delegate_task`、`manage_task`。`load_skill`仅供独立Pro车控层使用。`delegate_task`可选`query_mode=quick|research`与简短`spoken_goal`，完整要求保留在goal；只有明确要求详细朗读时`read_details=true`。`manage_task(action=get,task_ids=[...])`可一次读取多个明确任务，与单个task_id互斥；数组须非空、不同且全部属于当前会话，任何无效ID整批拒绝。reply/update/cancel仍仅支持单个task_id，用户回答已有澄清时reply原任务，不另建重复任务。

Talker跨轮保持15个热车控、3个调度和3个地点/天气工具；Pro加载仅在该委托上下文保留。模型上下文只压缩副本：历史任务保留目标、条件、状态、问题和口语结果，移除重复车况/上下文/工具schema；原始消息、参数、全部报告和trace保持完整。读取车况工具后由同一Talker解释具体值；混合读写在后续失败时仍确认已实际执行的动作，避免把部分成功描述成完全未执行。

后台天气工具`get_weather_forecast(city,date,country_code?)`使用真实Open-Meteo城市地理编码与预报API，明确目标当地日期、IANA时区、摄氏单位与最低/最高温；歧义城市须澄清，错日期/空值/历史数据不可作为未来预报回退。结果的`forecast_records`与原始JSON可供核对，`web_evidence=forecast`或`verified`区分结构化数据与网页材料；这不代表模型已经逐句完成事实审核。用户要求指定官网时仍需实际读取官网，并说明日期不能核实等限制。接口依据[Forecast API](https://open-meteo.com/en/docs)与[Geocoding API](https://open-meteo.com/en/docs/geocoding-api)。

完成任务的result可包含spoken_summary，作为简短自然口语回流；result.summary/answer仍保存完整报告，sources与trace不缩略。该字段由同一次Thinker完成调用提供，不增加润色模型；旧结果缺字段时保留完整报告事实，仅清理Markdown、URL等显示语法。默认简短口语与单一关键问题由生成提示约束，服务端不按前几句、字数或关键词截取返回内容，也不默删额外问句中可能承载的条件。task.question原文仍保存在任务与轨迹中。

## 独立音频 WebSocket `/ws/{session_id}/audio`

服务器到浏览器的专用音频连接不占用车控/文字消息队列；同样只允许本机 Origin。会话不存在关闭码为 4404；慢消费者超过 256 个待发消息时收到错误并以 4408 断开，取消该播放。客户端配置、取消和播放状态经控制 WebSocket 发送。

| 消息 | 字段 / 含义 |
|---|---|
| audio.started（JSON） | session_id、turn_id、client_request_id、32 位十六进制 playback_id、sample_rate=24000、channels=1、format=pcm_s16le |
| 音频（二进制） | 前 32 字节为 ASCII playback_id；后续为非空、偶数字节的单声道 little-endian signed 16-bit PCM |
| audio.metrics（JSON） | turn_id、playback_id、metrics，部分消息带 client_request_id；包含供应商/适配器/服务端阶段诊断，不作为浏览器端到端时钟 |
| audio.ended（JSON） | turn_id、playback_id；供应商输入/输出已完成，浏览器可能仍有 PCM 未播放 |
| audio.cancelled（JSON） | turn_id、playback_id、reason |
| audio.error（JSON） | turn_id、playback_id、message；零音频、无效 PCM、供应商错误等明确失败并释放朗读占位 |

每次播放有独立 playback_id，二进制帧通过它关联 audio.started，再关联原 query。控制与音频连接之间没有交叉消息排序保证；前端用 client_request_id / turn_id / playback_id 关联，不能假设 turn.started 总早于 audio.started。后台主动回流没有 query 的 client_request_id，单独记录，不混入 query 延迟分母。

开启朗读后，actor在轮次开始时预热Speech连接，与模型路由重叠；首段可播文本到达才启动合成session，避免规划期间空会话超时。首阶段返回工具调用或短JSON聊天路由，不朗读任意模型前缀。确认聊天后以tools=[]生成正文，按句号/问号/感叹号/换行送TTS；额外模型调用计入延迟；若模型未按路由JSON返回，保守等完整无工具消息后整段朗读，不提前播混合流。工具阶段下发反馈、失败回执和任务登记模板均由真实结构事件触发。已部分播放后出错仍记失败。

取消时Worklet报告rendered_output_samples和played_ms；客户端在发送新input前等这一报告（最多150ms，超时不伪造进度）。服务端用Speech的sentence.started/finished对应PCM位置，只把已完整播放的句子放入后续模型历史；完整显示原文仍在审计记录中，没有字级时间证据时不推测半句。

新输入、停止、关闭朗读或最后一个控制/音频客户端断线会取消上游并清空浏览器播放；旧帧、旧播放 false/true 与错误 ID 不覆盖新轮。audio.ended 不释放有音频的朗读占位，匹配的 Worklet drained→playback(false) 才释放，后台结果等待当前播放结束。音频断线不取消模型回答或后台任务。

核心计时在同一页面的 performance.now 时间域：表单 submit→首个合法非零 PCM 到达；静音/空帧/元数据不计。另报首 PCM 入队、Worklet 首次真实样本/非零样本渲染的主线程通知观测。postMessage 入队不等于播放，Worklet 观测包含线程通知延迟且不是扬声器声学起点。最新每次query目标为≤1500ms；历史2000ms原始报告与统计保留，离线重评另列。全部提交分母、三条路径预算与采样边界见[音频验收](AUDIO_VALIDATION.md)。

## 独立识别 WebSocket `/ws/{session_id}/asr`

每次显式录音使用独立连接，同一会话最多一段；仅本机 Origin。会话不存在关闭码 4404。ASR 接口只做识别，任何 partial/final 都不会直接创建轮次或触发动作；前端在用户结束录音并收到有效 final 后，经原控制 WebSocket 发送一次 `input`。

| 方向 / 消息 | 字段及含义 |
|---|---|
| 客户端 asr.start | capture_id（1–80位字母/数字/下划线/连字符）、format=pcm_s16le、sample_rate=16000、channels=1 |
| 服务端 asr.ready | capture_id、format/sample_rate/channels、metrics；只表示上游连接与配置已发送，供应商可能随后拒绝 |
| 客户端二进制 | 非空、偶数字节的16kHz单声道PCM，每帧最多6400 bytes（200ms），总计最多60秒 |
| 客户端 asr.stop | capture_id；最后一帧PCM之后发送，只结束录音输入，仍等待完整云端终结 |
| 客户端 asr.cancel | capture_id；取消本次，关闭连接并丢弃内容 |
| 服务端 asr.partial / asr.final | capture_id、text（完整转写快照）、metrics；partial仅预览，final必须在stop之后且具完整请求终结标记 |
| 服务端 asr.error | capture_id、code、message（脱敏）；不提交本次转写 |

未结束录音时不因供应商某段 `definite=true` 提交。连接/发送和停止后等待各受配置超时限制，整次连接90秒上限；录音音频最多60秒。取消/错误释放录音占位；成功 final 保留到客户端实际发送 input 或显式释放，90秒租期兜底，避免恢复TTS前后台结果抢先朗读。trace记录 `asr.started/completed/cancelled/error`，最终文本与安全计时可回看，原始麦克风PCM不写入数据库。

前端导出的 `recognition_records` 记录麦克风请求、ready、首帧发送、停止、首partial、final、实际提交的同页时钟，并关联client_request_id。供应商metrics与浏览器时钟分开。原query→非零PCM起点保持最终文本真正提交，不用ASR首字替代模型或TTS延迟。详见[配置及验证边界](ASR.md)。

## 执行与隔离

`SessionActor` 主循环独占会话状态写入。Ark 网络生成通过 asyncio task 只投递事件，不阻塞主循环。工具命令/回执通过 command_id 对应；用户打断不能删除已执行记录。前端按 turn_id 丢弃旧文本与音频。

`Database` 每次操作短事务，WAL；任务修改立即推进 revision 并写 inbox，worker 在步骤边界消费。claim 排除本 worker 活跃任务，默认并发 2。旧 revision 乐观写失败；终态拒绝迟到完成；已取消不能被补充隐式复活。普通进度只更新卡片，主动结果需生成/朗读均空闲。

`ArkClient` 使用标准 OpenAI function calling；100个车控函数开启strict schema，Talker disabled thinking，Thinker enabled thinking。strict只约束结构与类型；会话层还对显式导航命令的完整地点原文做一致性校验，发生缩写或改名时拒绝下发且不修改参数。`TavilyClient` 真实 Search/Extract。`VolcanoTTS` 使用独立 Speech 凭证。任何缺凭证、超时、无结果和参数错误均显式反馈，无固定模型回复/搜索数据/替代声音。

2026-10-14 范围补充：`delegate_task` 的 `query_domain=weather` 在当前原型仅支持显式 `query_mode=quick`。其他/缺省mode返回 `weather_quick_required`、`retry_model=true`、`executed=false`，登记前拒绝且不改写参数；同一Talker在原5次上限内继续。`general` research协议保留。明确指向已有任务/历史查询结果的读取请求须先取得本轮真实get证据；错误直答仅存原模型轨迹，不展示或合成。已完成任务引用与主动历史播报保留归属；这些检查不是通用自然语言意图或事实验证。


任务新增持久字段：`running_seconds/running_since/clarification_expires_at/checkpoint`。300秒仅计算running累计，waiting_clarification单独默认1800秒；回复或补充条件增加revision并恢复完整checkpoint的messages/sources，追加user条件。未完成的工具组和旧revision网络结果不能提交到新版。车辆`_meta.timezone`进入Talker和Thinker上下文，UTC缺省，可显式配置为用户或车辆所在地。

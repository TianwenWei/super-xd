# 原型接口

本文描述本地Demo HTTP/WebSocket与前端事件；生产内部NLP接口见[Gateway契约](GATEWAY_CONTRACT.md)。Demo经SessionActor→NLPBridge→NLPService运行同一推理核心，SQLite、音频和模拟设备属于本地适配层。

Talker固定26工具（15热车控、3调度、3地点/天气、网页、车书及3媒体/导航），其余85个车控由前台Reasoner规划，不追加到Talker工具集合。Planner使用原话、车况、音区与真实已完成动作，只开放load_skill/submit_vehicle_plan；意图span、参数、依赖和保持约束由代码校验，语义仍由模型理解。具体账本和执行规则见[NLP核心](NLP_HARNESS.md)。

完整工具组校验后才交接；模拟器独立执行并返回receipt_id、实际状态和版本，浏览器ACK不代表成功。前瞻预告可流式播放，成功已有预告时不重复播报，失败/unknown明确反馈。前端分别显示原生vehicle_calls、Planner动作及实际回执；没有对应回执不得把plan.ok或预检当执行。刷新/重连从session.snapshot恢复当前会话状态，不伪造服务重启后的历史会话恢复。

公共 Demo 类型在 `src/super_xd/demo/contracts.py`，共享目录及人设类型在 `src/super_xd/nlp/contracts/catalog.py`，前端类型在 `frontend/src/types.ts`。

## HTTP

- `GET /api/health`：模型连接预热与服务健康；`manual` 为独立车书索引状态。
- `GET /api/config`：persona、100函数/10skill/热工具、模型、TTS及ASR配置状态；`manual` 包含向量模型、密钥是否配置、索引就绪情况、标题、页数和片段数。无密钥。
- `GET /api/manual/pdf`：当前车书原始 PDF，`application/pdf` 内联返回；前端可加 `#page=<page>` 打开命中页。
- `GET /api/manual/{manual_id}/pages/{page}.png`：当前车书合法页的原始 PDF 整页 PNG；未知ID/越界页码404，预览不可用503。
- `POST /api/sessions`：创建 session_id。
- `DELETE /api/sessions/{session_id}`：关闭并移除内存会话，返回`{closed:true}`；不存在返回404。释放会话资源，保留SQLite记录，不等于取消已登记的研究任务。
- `POST /api/sessions/{session_id}/vehicle/commands/{command_id}/execute`：执行已登记模拟命令；按command_id幂等。默认200ms下行、300ms执行、200ms上行；返回receipt_id、ok、reason_code、resume_condition、state、state_version、timings。未知命令拒绝，浏览器只能把该回执ID回传。
- `GET /api/sessions/{session_id}`：车况、任务、近期审计 `history`（最近 12 轮），以及 `turns` 轮次汇总（turn_id/text/output/finished/proactive/task_id/persona_version/metrics）。完整持久化记录通过 traces 接口读取。
- `GET /api/sessions/{session_id}/traces`：模型实际输出与 Agent 轨迹，支持增量分页和 turn/task 筛选，详见下节。

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
- `policy.rejected`及工具错误：记录结构、来源或能力拒绝的完整结果，`executed:false`不能当成功回执。空会话误用manage_task可返回no_task/retry_model，由同一Talker在预算内重新选择；不使用针对原句的关键词补丁。
- `vehicle.dispatched` / `vehicle.receipt`：命令 ID、参数、实际模拟结果、耗时或超时；旧轮次的迟到回执标 stale，仍保存真实执行结果。ACK 不是成功回执，重复 ACK 不产生第二次执行或回执记录。
- `turn.started` / `turn.interrupted` / `turn.completed`：输入、打断或最终输出与 metrics。turn.completed 的 output_source 区分 model_content 与 persona_template；工具成功提示、拒绝和后台回流模板不会冒充模型原文。
- `task.*`、`revision.*`、`step.*`：任务生命周期、条件版本变化及 Thinker 执行步骤；记录旧版本的观察结果不表示该版本结果已被采纳。

content=null 表示供应商的空内容/仅工具调用；usage=null 或字段缺失表示未返回，不能解读为 0 token。reasoning_content 只展示供应商实际返回的内容；未返回时显示未提供，不推测、补写或宣称访问内部推理。原始返回内容不以摘要替代，脱敏除外；轨迹功能启用前未采集的供应商元字段无法补回。

Ark流请求使用stream_options.include_usage=true，但usage仍以实际返回为准。流式meta.response_headers_ms从整个逻辑调用开始累计至最终尝试收到headers，包含此前失败尝试；attempts[].response_headers_ms才是各次尝试相对自身开始的时间。顶层elapsed_ms、first_chunk_ms、first_delta_ms、first_tool_delta_ms和first_output_ms同样按逻辑调用累计，各attempt内相应字段按单次尝试计时。未观测到的时间保持null或缺失，不能用后续成功抹掉先前失败。

headers等待包含请求准备、连接池、建连、上传、网络/代理、供应商接入及服务处理等尚未分解的耗时，不能等同prefill或模型推理；客户端完成send_body也不能证明对端已接收、解析完请求。first_tool_delta_ms记录首个非空工具片段，tool-only时first_delta_ms可为null；service_tier只记录供应商值。正常SSE遇[DONE]后读至HTTP EOF供连接池复用，取消仍可关闭流。本地真实TCP回归已验证同一连接的顺序复用，不能据此补造旧线上请求的复用或缓存命中证据。

Talker 用独立队列和线程异步写入，SQLite 日志操作不阻塞前台事件循环；Thinker 在独立 worker 中以短事务写入。读取可能短暂落后于流式文字或回执。`recording_error` 非 null 表示曾发生写入失败，界面明确提示记录可能不完整，不能把缺失事件当作没有调用。

记录位于本机 SQLite（默认 `data/super_xd.sqlite3` 的 trace_events），持久化时递归脱敏凭证字段、已知环境密钥值与 Bearer 文本，不记录客户端认证请求头。用户对话、系统上下文、模型输出和工具结果仍为本机明文调试数据；未提供日志加密或生产级鉴权。数据库持久化不改变当前只验收连续在线会话、未实现完整服务重启恢复的范围。

前端顶部、助手消息及任务卡提供“模型输出 / Agent 轨迹”入口；抽屉支持全会话/轮次/任务范围、模型原文/完整轨迹、关联调用筛选、完整事件 JSON 和复制。展开时按游标拉取全部分页并轮询新增记录，关闭保留缓存，切换会话清空缓存；轨迹读取失败不清除已读记录。

核验方法见[轨迹核验](TRACE_VALIDATION.md)。旧模型回调是否 stale 由 actor 消费顺序决定，网络接收时间单独保留；已完成的旧模型输出仍可回看，不会因新输入先进入队列而误标为当前已采纳结果。

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
| vehicle.configure | settings：speed_kph、gear、navigation_active、call_active、timezone（IANA）、location、location_country；P档不能有车速。地点为用户提供的城市或地标，空字符串清除默认位置 |
| vehicle.receipt | command_id、receipt_id；必须匹配模拟执行端真实保存的回执，拒绝自报成功与旧ACK |
| audio.played | turn_id、playback_id、played_ms；AudioWorklet真实渲染样本数换算，支持已打断轮次归档 |
| attention.set | in_call、demanding_driving可选boolean；Demo注入注意力事实以验证播报仲裁 |
| task.create | goal |
| task.get | task_id 可选；多个任务不明确则返回消歧 |
| speaker.set | seat 必填：driver / front_passenger / rear_left / rear_right；更新会话默认音区，返回speaker.changed；从下一轮生效 |
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
| lookup.result | turn_id、call_id、tool、result；前台 POI/天气/网页/车书实际查询结果，按 call_id 合并 |

### 地点与天气 FC

`GET /api/config` 的 `maps` 字段仅返回配置是否存在、提供方及模拟导航范围。每轮 `session.snapshot.turns[].lookup_results` 保留 `{call_id,tool,result}`，便于重连还原卡片。完整结果进入 `tool.completed` 轨迹；无后台任务与 Thinker 轮询。

| FC | 参数 | 结果及行为 |
|---|---|---|
| search_places | query：含城市/区域的文本；max_results：1–5，默认5 | Google Places (New) 返回 places，每项含 place_id/name/address/location/country_code/maps_url/attributions；不修改导航 |
| get_weather | location：城市及国家；date：now/today/tomorrow/YYYY-MM-DD | Google Places 城市消歧后获取 Google Weather；结果含 provider/place/date/timezone/mode/summary/details/queried_at/source_url/notice |
| start_poi_navigation | place_id：本会话真实搜索结果 ID | 复用 set_navigation_destination 模拟命令/回执；成功后 navigation.destination_place 保存所选地点；无实际路线 |

### 前台网页搜索 FC

`web_search(query, max_results?, include_domains?)` 使用服务端 `TAVILY_API_KEY` 调用 Tavily Search，默认返回三个结果，最多五个。`include_domains` 可选，仅接受域名列表。`GET /api/config` 的 `web_search` 字段返回提供方与凭证是否配置，不返回密钥。

结果经同一 `lookup.result` 事件返回，包含 `ok/kind/provider/query/results/queried_at/evidence/notice/elapsed_ms`。每个搜索结果保留 Tavily 返回的标题、URL 和摘要；`evidence=search_snippets` 表示未读取或核实页面全文。工具结果回给同一个 Talker 组织口语答案，前端保留来源链接、摘要和查询时间，不创建后台任务。

网页查询和地点/天气查询共用异步执行、25秒整体超时和过期轮次丢弃机制。缺少密钥、上游错误、超时直接返回错误；不生成替代搜索结果。复杂比较、多步调研及指定来源深入核验仍使用 `delegate_task`。

### 车书问答 FC

`search_manual(query, max_results?)` 在当前轮检索本地只读车书库，默认返回五段，最多五段。仅问题文字发送给火山 embedding；本地执行向量比对，随后把真实原文作为工具结果交给同一个 Talker 回答。没有额外路由模型，也不登记任务或执行车控。与其他 lookup 共用取消、25秒整体超时和过期结果丢弃机制。

`lookup.result.result` 使用 `kind=manual, evidence=manual_text`，包含 `manual:{id,title,pages,pdf_url}` 和 `hits:[{chunk_id,page,section,text,score,images:[{url,caption,page}]}]`。PDF/图片均为上面的同源车书接口；图片为文字命中页的原始整页预览，仅在渲染成功后提供，失败保留 `images:[]` 与 `image_notice`。`notice` 保留资料版本和文字提取限制。`embedding_ms/local_search_ms/cache_hit` 用于调试；分数不是事实可信度。

完整 embedding 请求/响应进入脱敏的 `embedding.completed` 轨迹，不包含认证头；原始向量不会放入回答模型上下文或前端 lookup 事件。配置、重建与验收记录见 [MANUAL_RAG.md](MANUAL_RAG.md)。

日期按地点 IANA 时区计算，并匹配 Google `displayDate`（Google 日预报区间为当地07:00至次日07:00）。指定日期限定当地今天至未来第9天；请求日期无记录则返回错误，绝不拿别的日期替代。城市多解返回 `needs_clarification/candidates`，不默选第一项。天气返回404时才改用 Open-Meteo，显式标注 `provider=Open-Meteo`、`fallback_reason=google_unsupported_location`；权限/配额/超时均直接报错。原始天气字段保留在 details，summary 使用摄氏度，Open-Meteo WMO代码附中文释义。

查询协程不阻塞 actor 收件；整次 FC 最长25秒，HTTP单次超时10秒。用户打断取消在途查询，旧轮结果不进入候选注册表。注册表仅保留本会话最近50个真实地点；虚构/失效 ID 不下发导航。普通文字目的地更新和取消导航会清除旧 destination_place。所有 Google 密钥仅经服务端请求头发送，轨迹脱敏。

主服务读取环境或项目 `.env` 的 `GOOGLE_MAPS_API_KEY`，不执行 `.zshrc`。验收脚本可安全提取 `.zshrc` 中的字面量赋值。接口依据 [Places Text Search](https://developers.google.com/maps/documentation/places/web-service/text-search)、[Google Weather](https://developers.google.com/maps/documentation/weather)、[覆盖范围](https://developers.google.com/maps/documentation/weather/coverage)和 [Open-Meteo](https://open-meteo.com/en/docs)。

单次纯 POI 查询的最终口述列表由真实结果按原序生成，与卡片序号及 `recent_places` 一致；避免模型自行重排后“第一个”选错。原模型文字保留在 model.completed，最终来源标记为 `verified_place_results`。直接“导航到明确地点”在查到唯一候选后继续调用导航 FC，收到车端回执即结束，无额外润色模型。

任务状态：queued / running / waiting_clarification / cancelling / completed / failed / cancelled；awaiting_confirm只作契约预留，当前未接不可逆工具确认执行链。task含id/delivery_mode/goal/constraints/context/status/revision/summary/result/question/error/steps/sources/created_at/updated_at。context包含登记时车况、近期对话、活跃任务与时区，以及按revision保留的澄清问答。来源包含真实工具生成的 URL、queried_at、extracted 状态。

Talker调度工具为固定三个：`delegate_vehicle_control`、`delegate_task`、`manage_task`。`load_skill`仅供独立Pro车控层使用。`delegate_task`可选`mode=foreground|background`、`query_mode=quick|research`与简短`spoken_goal`，完整要求保留在goal；只有明确要求详细朗读时`read_details=true`。`manage_task(action=get,task_ids=[...])`可一次读取多个明确任务，与单个task_id互斥；数组须非空、不同且全部属于当前会话，任何无效ID整批拒绝。reply/update/cancel仍仅支持单个task_id，用户回答已有澄清时reply原任务，不另建重复任务。

Talker跨轮保持固定26个工具；Pro加载仅在该委托上下文保留。模型上下文只压缩副本：历史任务保留目标、条件、状态、问题和口语结果，移除重复车况/上下文/工具schema；原始消息、参数、全部报告和trace保持完整。读取车况工具后由同一Talker解释具体值；混合读写在后续失败时仍确认已实际执行的动作，避免把部分成功描述成完全未执行。

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

开启朗读后，Speech建连与Talker生成重叠，模型文字按句增量送TTS；一次Talker调用同时决定工具与表达，不做短JSON路由后再调用tools=[]的两段生成。完整工具组验证前不执行动作；预告只表示正在做，完成以真实回执/任务登记确认为准。成功已有预告时不重复朗读，失败或unknown补充真实反馈；已部分播放后出错仍记失败。

取消时Worklet报告rendered_output_samples和played_ms；客户端在发送新input前等这一报告（最多150ms，超时不伪造进度）。服务端用Speech的sentence.started/finished对应PCM位置，只把已完整播放的句子放入后续模型历史；完整显示原文仍在审计记录中，没有字级时间证据时不推测半句。

新输入、停止、关闭朗读或最后一个控制/音频客户端断线会取消上游并清空浏览器播放；旧帧、旧播放 false/true 与错误 ID 不覆盖新轮。audio.ended 不释放有音频的朗读占位，匹配的 Worklet drained→playback(false) 才释放，后台结果等待当前播放结束。音频断线不取消模型回答或后台任务。

核心计时在同一页面的 performance.now 时间域：表单 submit→首个合法非零 PCM 到达；静音/空帧/元数据不计。另报首 PCM 入队、Worklet 首次真实样本/非零样本渲染的主线程通知观测。postMessage 入队不等于播放，Worklet 观测包含线程通知延迟且不是扬声器声学起点。最新每次query目标为≤1500ms；旧口径的历史数据不能充当当前验收结果。全部提交分母、三条路径预算与采样边界见[音频验收](AUDIO_VALIDATION.md)。

## 执行与隔离

`SessionActor` 主循环独占会话状态写入。模型网络生成通过 asyncio task 只投递事件，不阻塞主循环。工具命令/回执通过 command_id 对应；用户打断不能删除已执行记录。前端按 turn_id 丢弃旧文本与音频。

`Database` 每次操作短事务，WAL；任务修改立即推进 revision 并写 inbox，worker 在步骤边界消费。claim 排除本 worker 活跃任务，默认并发 2。旧 revision 乐观写失败；终态拒绝迟到完成；已取消不能被补充隐式复活。普通进度只更新卡片，主动结果需生成/朗读均空闲。

`ModelService`只接受`ModelRequest`并按冻结角色绑定模型；结果为`ModelResult(message, metadata)`或`ModelEvent`流，没有旧thinker布尔参数或assistant消息中的_meta包装。`ChatCompletionsTransport`统一HTTP/SSE、累计预算、重试与取消；Ark、Vertex、Compatible仅负责认证和payload。100个车控契约的strict/schema只约束结构与类型，来源和权限仍由策略精确核对。`TavilyClient`位于reads适配器，负责真实Search/Extract；音频使用独立Speech凭证。缺配置、超时、无结果和非法参数均明确反馈。

`delegate_task.mode=foreground/background`只决定交付；`query_mode=quick/research`决定检索策略，均使用同一只读TaskRunner。前台先登记并等待独立Worker推进，默认8秒后对同一任务执行delivery-mode CAS；task_id、revision、lease、checkpoint、证据和预算不变。当前没有跨worker租约移交。任务在前台已交付后按版本去重；用户打断只是解除等待，取消研究仍需manage_task cancel。

通知由actor中的NotificationQueue和纯speaking策略决定。旧revision/status或到期消息丢弃，progress只上屏；生成、ASR、朗读、断线、通话或繁忙驾驶期间延后。Demo可通过`attention.set`输入布尔`in_call/demanding_driving`事实，不自动推断；该事件是集成测试入口。普通结果有效期10分钟，澄清使用clarification_expires_at，重复事件不延长。展示数据读取失败从0.25秒退避至最多15秒，并受通知有效期约束；旧回调不能触发重复播报。事件表达使用确定性路径，不额外调用Talker模型。

SessionRegistry每60秒回收闲置30分钟、没有控制/音频连接、无生成且没有queued/running/cancelling/waiting_clarification任务的会话。回收只释放内存与会话资源；SQLite记录保留，旧session_id不会自动重建。

历史工具级extra_content和assistant顶层extra_content分别写入HistoricalCall和AssistantRecord.provider_metadata；Demo桥接、checkpoint恢复和历史编译保留供应商opaque续推字段，不能改写或把它当作执行权限。

2026-10-14 范围补充：`delegate_task` 的 `query_domain=weather` 在当前原型仅支持显式 `query_mode=quick`。其他/缺省mode返回 `weather_quick_required`、`retry_model=true`、`executed=false`，登记前拒绝且不改写参数；同一Talker在原5次上限内继续。`general` research协议保留。明确指向已有任务/历史查询结果的读取请求须先取得本轮真实get证据；错误直答仅存原模型轨迹，不展示或合成。已完成任务引用与主动历史播报保留归属；这些检查不是通用自然语言意图或事实验证。


任务持久字段：`delivery_mode/running_seconds/running_since/clarification_expires_at/checkpoint/deadline_at/deadline_paused_at/progress/progress_notified/error_code`。从登记开始最长60秒，排队计入；waiting_clarification暂停截止时间，仍有独立的默认1800秒澄清期限。补充条件、revision推进和进程重启不重置已用预算。30秒尚在queued/running时，事务内写一次`progress_notice`事件，仅更新屏幕；终态取代旧进度。最后15秒取消尚未结束的常规调用，预留一次关闭思考、仅可finish_task的收尾调用；60秒仍未得到合法结果则失败，保留真实来源与checkpoint，拒绝迟到提交。工具预算不消耗交付调用，最后一步同样仅允许交付。`result.partial=true`标明预算收尾的阶段结果，不表示完整需求已全部核实。技术error/搜索参数保留在DB、卡片诊断和trace；模型任务上下文与默认TTS使用结构化阶段、来源读取数量和error_code对应的口语，不回放原始异常。

播放打断信息在快照`interrupted_playback`内记录，历史assistant正文保留完整生成文本，`playback_history`分别记录播放状态和已确认听到的片段，不覆盖原始生成记录。车辆`_meta.timezone`进入Talker和Thinker上下文；初值来自`DEFAULT_TIMEZONE`（未配置时UTC）。`DEFAULT_LOCATION`与`DEFAULT_LOCATION_COUNTRY`设置用户提供的默认位置，未配置时不猜地点。每次Talker请求在历史之后的会话快照中注入`current_location:{name,country,source:user_setting}`（未知时null）、同一时刻的`current_utc/current_local/current_date/timezone`，当地时间由IANA时区转换并处理夏令时。用户提供的位置设置也作为地点参数的来源；后台任务保留位置来源与登记时车况，Thinker仍使用执行时的UTC时间和任务时区。固定prompt前缀和工具集保持稳定，不新增模型调用。

限时收尾使用快速模型并保持中性表达；其临时`finish_task` schema只接收`source_ids`，从本任务实际读取/验证的来源账本中选择。服务端将ID映射成真实URL并附到阅读报告，模型不需要拼接网址，原有来源校验继续执行。原始模型ID参数、解析后的URL参数、工具回执均进入trace。收尾调用不再启动搜索，也不因校验失败反复调用模型；校验失败使用`finalization_failed`友好说明，保留已取得资料。

### 用户座位 / 音区

`session.snapshot.speaker`和`speaker.changed.speaker`包含`seat/audio_zone/display_name/default_targets/control_defaults`。模拟会话默认主驾（可用`DEFAULT_SPEAKER_SEAT`配置上述四个值之一），前端“当前音区”可切换。当前原型没有麦克风声源定位，座位来自会话设置；集成方可在`input`事件携带`speaker_seat`，只覆盖本轮，不改变会话默认值。非法值在启动轮次前拒绝。

每轮开始固定speaker并写入`turn.started`轨迹。Talker将其放在历史之后、本轮输入之前的system会话快照；Vehicle Planner另有动态system音区消息，用户原话不重写；后台任务保存登记轮次音区，Thinker也以system数据接收。人设不改变音区或默认目标，Talker工具集仍固定26个。

| 音区 | 座椅 / 车窗 / 车门 / 阅读灯 | 温区 | 车内照明 | 后视镜侧 |
| --- | --- | --- | --- | --- |
| driver | driver | driver | front | left |
| front_passenger | front_passenger | front_passenger | front | right |
| rear_left | rear_left | rear | rear | left |
| rear_right | rear_right | rear | rear | right |

未明确位置或“我的 / 我这边”使用以上默认位置；明确座位、全部范围和近期明确指代优先。儿童安全锁默认同侧后门，玻璃清洗默认所在排对应的前/后挡风玻璃。区域粒度受真实工具schema限制；不为整车音量、雨刷速度、方向盘等臆造位置参数。

`control_defaults`从工具参数schema生成，当前覆盖23个带位置参数的写操作，提供底层功能的默认位置参数以及热工具/Vehicle Planner路线；它不是新增的可调用工具清单，也不授予额外权限。Thinker只接收座位与默认区域，不接收执行路线表。位置决定由模型按prompt/few-shot和这些结构化数据完成，代码不匹配用户文本，不偷偷改写模型参数，也不增加模型调用。温区同步等联动会改变其他区域时先澄清，音区本身不授权额外动作。

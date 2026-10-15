# Thinker harness

`python -m backend.worker` 启动独立 Python 进程，共用云端会话服务的 SQLite WAL 数据库。完整研究默认 Ark Thinker 是 `doubao-seed-2-1-pro-260915`，启用 thinking。真实网络查询使用服务端 `TAVILY_API_KEY` 调用 Tavily Search 和 Extract。生产运行没有假模型或假网页查询回退。

默认并发 2，其余任务排队。Talker 使用另一个客户端和执行路径，不占用 Thinker 的并发额度。配置 `THINKER_CONCURRENCY`、`THINKER_MAX_STEPS`、`THINKER_TIMEOUT_SECONDS`；后两项分别限制在最多 20 和 300。5 分钟从任务创建算起，包括排队、修改和等待澄清，回复不会重新获得时间预算；轮询会将过期任务终止。修改或澄清后续跑仍沿用已经消耗的推理步骤。

`delegate_task` 将同次 Talker 模型选择的 `query_mode` 放入 `task.context.query_mode`，worker 不以关键词路由。`research`（缺省及未知值）沿用完整研究流程、Pro thinking 和上述预算。`quick` 使用当前 `TALKER_MODEL` 并关闭 thinking（不固定为 mini，也不另做模型扫描），适用于简短实时核实，通常只需两次模型调用：第一次调用结构化 `get_weather_forecast` 或真实 `lookup_web`，第二次根据实际材料一次 `finish_task` 提交简短完整答案与口语结论。quick 提供 forecast、lookup、澄清和完成工具；缺失关键字段或来源冲突时可再核对一次，全任务最多两次 lookup、四次模型调用，不以固定文本或模型以外的规则拼出答案。报告篇幅是生成偏好，不截断任何原始模型/网页材料或必要条件。quick 每步要求真实函数调用；已用完两次 lookup 或到达第四步时，仅提供并指定 `finish_task`，保留一次纠正校验错误的收口机会。强制提交也可以是带明确无法核实限制的结论，不能编造事实。

quick 另有累计执行 45 秒预算，模型请求、工具请求和执行编排计入；排队及等待用户澄清不计入这 45 秒，但仍计入创建起五分钟绝对硬限。每次执行的实际耗时写入 `task.execution.finished` trace，回复后从持久轨迹累计已有消耗，revision 修改不会刷新预算。达到步骤或时间预算明确失败，不把未提交结果称为完成。 两次 lookup 的全任务预算跨 revision 保持累计，但完成资格只来自当前 revision 的真实工具尝试；参数校验失败不算真实尝试。新条件清空旧证据后若两次 lookup 已用尽，直接明确失败并说明新条件尚未核实，不调用模型强行生成无依据结果，也不刷新预算。

`lookup_web(query, max_results?)` 真实调用 Tavily Search（默认最多 3 条），按搜索返回的相关性排序对前两个有效且不同的 URL 执行真实 Extract，再将完整搜索与页面内容一起交给模型。全量搜索来源仍保留，模型通过 `source_urls` 指出结论实际采用的来源；未读取的来源若被引用仍明确标记未核实。Search 和 Extract 之间读取最新 revision/cancel；旧搜索的实际返回另存 stale 工具 trace，不会继续按旧条件提取页面。两个真实子调用独立记录工具轨迹并通过 `parent_call_id` 关联 lookup，失败与超时保留实际限制。结果包含 `query_mode` 标识，其他来源、口语与完整报告契约保持。

首版按最新范围不做车辆工具检索。系统提示词直接列出目录生成的 10 个 skill 分类 ID 与中文名称；Thinker 根据分类 ID 调用 `load_skill` 获得完整函数说明和 schema，无需加载模型或向量缓存。

标准函数工具：

| 工具 | 行为 |
| --- | --- |
| `search_web(query, max_results?)` | 真实搜索，最多 5 条，记录服务端查询时间和实际返回的来源 |
| `extract_web(urls)` | 读取本任务已经真实搜索得到的 http(s) 页面，最多 5 个 |
| `get_weather_forecast(city, date, country_code?)` | 模型选择真实 Open-Meteo 城市地理编码和目标日期的结构化温度预报；同名城市澄清，错误或历史数据不当预报 |
| `load_skill(skill_id)` | 按系统提示词中的分类 ID 直接查看车辆子系统说明与完整函数 schema |
| `ask_clarification(question)` | 持久化问题，进入等待澄清状态并释放并发名额 |
| `suggest_vehicle_action(name, arguments, reason)` | 验证目录与参数，只保存建议，不执行车辆动作 |
| `finish_task(summary, spoken_summary?, source_urls?)` | 一次提交完整中性报告、简短口语结论及真实来源，完成任务 |

所有工具参数经过 JSON Schema 校验。未知函数、越界参数、未搜索的提取 URL、虚构引用会返回结构化错误供模型修正。每次工具调用受 `TOOL_TIMEOUT_SECONDS` 限制，超时作为明确失败回传模型；整个任务仍受绝对硬时限约束。已经找到网页来源时，至少先尝试一次 Extract 才能完成；结构化 forecast 来源直接来自真实 JSON API，不要求再 Extract；提取失败会保留失败记录并明确标记未核实。来源和时间来自工具返回的数据，不接受模型提供的来源元数据。没有结果或凭证错误时不会制造替代网页，最终结果附上“真实信息工具未取得可核实来源”的限制。

Thinker 不包含 persona。结果结构包括 `summary`、`spoken_summary`、`sources`（URL、标题、查询时间、是否读取成功）、`suggested_actions`、`query_times`、`web_evidence` 和 `tool_observations`。`summary` 保留完整阅读报告，`spoken_summary` 供 actor 默认朗读：提示模型用中性自然口语，第一句直接说最有用的具体结论，通常 1–3 句、30–100 字，不含 Markdown、列表、表格、URL、引用标记、JSON 或任务/工具调试信息。两者在同一次 `finish_task` 模型调用中生成，没有额外润色调用。句数与字数是生成偏好，不是服务端裁剪规则。为兼容旧调用，省略口语字段时，共享 `brief_result` 保留真实报告的全部事实，仅清理显示格式；不按前几句、固定字数或关键词筛选内容。朗读字段经共享语法清洗，真实网页未核实或动作尚未执行的必要限制仍会保留；原始模型参数、完整报告与工具结果留在 trace。用户明确要求详细朗读时，actor 可选取完整 `summary`，不改变报告内容。

`web_evidence` 为 `extracted`、`search_only`、`none`、`forecast`（纯结构化预报）或 `verified`（结构化预报与已读取网页混合）。这些字段描述材料取得方式，不代表额外模型事实审核。forecast 来源的 `kind=forecast`，`extracted=false`，来源时间、目标日期、时区和摄氏单位全部来自工具；`forecast_records` 保存完整可核对的结构化地点、日期、温度与来源。工具响应和原始 geocoding/forecast JSON 全量留在 transcript/trace。 实际返回的 `geocoding_source_url` 同时登记为 `kind=geocoding`、`supports=location`，可作为城市出处与真实 forecast URL 一起引用，无需对这两个 JSON API 再 Extract；地理编码不能单独支撑温度预报。任意未返回的 URL 仍被拒绝，完成校验失败会提供精确 `citable_source_urls` 供模型修正。历史天气、气候平均或陈旧页面不能代替未来预报；明确要求气象局官网时仍须实际读取该官网，官网无法证明目标日则如实说明，不能把 Open-Meteo 冒充指定官网。完整标准 assistant `tool_calls` 和 tool `tool_result` 消息保存在任务 `transcript`。建议由会话 actor 结合最新车况校验，只有模拟车端真实执行回执才能表示已执行。澄清仍使用 `question`，提示词要求只问一个必要的关键问题；失败沿用 `error`/`summary`，由 actor 用当前人设简短表达。

完整过程另存 SQLite `trace_events`，独立于最新 `transcript`，不截断模型或工具正文。每次模型调用记录 `model.started`、`model.completed` 或 `model.error`，返回消息与供应商 `_meta` 分开保存；meta 包括真实返回的模型标识、usage、finish reason、计时及原始响应。只记录供应商实际返回的 reasoning，不生成或补造内部推理；meta 不会进入后续模型消息。工具记录 `tool.started`、`tool.completed` 或 `tool.error`，包括完整参数、真实结果和错误。每次模型请求有独立 call ID，工具通过 `model_call_id` 关联该请求；task、revision、step 与登记时的 `origin_turn_id` 连接前后台过程。请求认证、密钥配置不采集，诊断中的认证字段与已知环境密钥会脱敏。

任务创建、运行、澄清、回复、条件修改、取消、失败与完成同时生成 `task.*` 生命周期 trace，与原有状态事件在同一短事务提交。条件修改或取消后才返回的模型/工具内容仍可回看，标记为 `stale`；`revision.discarded` 记录新 revision 和丢弃原因，它们不会成为已采用的任务结果。`Database.traces_after` 按 session 隔离，支持 turn/task 过滤和 trace ID 游标分页；记录包含发生时间，旧 revision 永不覆盖。

每次外部调用前后均消费 inbox 并读取当前 revision。模型或工具仍在处理旧条件时收到修改，旧响应不会提交到新 revision；下一步骤基于原始目标和最新完整 constraints 重建提示词。时间、步骤继续累计。`save_task` 使用 revision 乐观比较；终态及已取消任务不能被迟到结果复活。SQLite 事务不会跨越模型、网页或检索 await。

进程仅负责任务状态和事件，事件进入会话 actor 后才影响 UI 和播报。进度默认只更新任务卡片；等待澄清及最终结果由 actor 在当前生成与朗读结束后用当前 persona 表达。切 persona 无需重建 Thinker 或任务。此原型只验收连续在线；进程重启时恢复已运行任务不在本次范围内。

离线回归：`.venv/bin/python -m pytest tests/test_worker.py -q`。fake Ark/Tavily 仅通过测试显式依赖注入，覆盖实际工具消息保存、来源白名单、修改竞争、取消、澄清释放名额与回复、并发排队、参数错误、凭证错误脱敏、无结果、时间与步骤预算，以及动作建议不执行。真实验收记录位于 `docs/thinker-live.json`，其中标注真实模型和查询调用，不能把离线测试当作真实 API 通过结果。

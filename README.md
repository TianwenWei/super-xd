# Super XD — Talker / Thinker 车载语音原型

本机 React 车机验证台 + FastAPI 会话 actor + 独立 Python Thinker。开发重点是云端 ALP / agent harness。文字输入或火山 ASR 最终转写进入 Talker；车辆全部模拟，实时信息使用真实 Ark + Tavily。默认模型没有 mock 替代。

当前核心延迟目标是**每次浏览器提交 query 到收到首个真实非零 PCM ≤1.5 秒**，同时保留语义正确性；另报告 Worklet 首次渲染。文字首字、模拟车控回执与供应商首包均为诊断指标，不能替代这个端到端目标。已有口语专项按新门槛为0/7，当时最新部署4次为0/4；这是旧观测离线重评，不是新测量。真实批次与所有超标/失败见[音频验收](docs/AUDIO_VALIDATION.md)。

按用户最新要求，1.5秒保留为期望与优化预算，**不阻塞本轮原型交付**。未达标样本和测量计算如实保留，不为达成数字继续扩大测试或改写结果；当前已完成的功能与优化保留。

**历史2秒口径也未达标**：口语调整前15次代表性浏览器请求中3次≤2秒；连同2次刻意取消/仲裁验证，全部17次提交为3/17（17.6%），首非零PCM观测P50=2182.35ms、P95=3817.525ms。按新1500ms门槛，旧批次分别仅1/15、1/17。原JSON与2秒统计保留，真实增量播放、车控回执和取消仲裁通过不代表延迟通过。

历史短口语专项7次全部正常播完，按当时2秒门槛为3/7；其中最新部署4次为1/4，P50/P95=2783.15/4312.6ms。前三次属于中间部署，独立标明。实际温控已缩为“空调调成二十三度了。”，研究同次返回58字口语与678字完整报告；模型等待仍造成超标，详见[专项与真实阶段证据](docs/AUDIO_VALIDATION.md)。

给定prefill500ms、200token/s与TTS400ms，预算为`500 + 5×N_safe + harness/传输 + 400 ≤1500ms`。N_safe包含安全路由前全部生成token，含工具名、完整合法参数及协议结构，不能只算最终短句。使用40–60个理想预算token时，基线1100–1200ms，余量400–300ms；这不是汉字换算或实测保证。车控下发即播进行中反馈，成功静默、失败补报；委托须真实登记；开启朗读的聊天先用短路由确认，再在禁用工具的第二阶段按句送入TTS，见[三条路径预算](docs/AUDIO_VALIDATION.md#当前15秒目标的业务预算)。

[阶段诊断](docs/AUDIO_VALIDATION.md#已有样本的阶段诊断与时钟边界)分别报告服务器、Speech和浏览器时钟。闲聊与委托的模型返回前时间约占服务器首PCM等待91%/89%；建连大多已与模型生成重叠，不能重复相加。旧Ark流的HTTP复用风险和usage缺失单独说明，不用连接池配置冒充实际复用证据。

## 运行

需要 Python 3.12、Node.js 22+，推荐安装 uv。

```bash
cd /Users/weitianwen/PycharmProjects/super-xd
cp .env.example .env  # 若进程环境已有密钥则不必复制；填入服务端配置
./scripts/start.sh
```

打开[车机验证台](http://127.0.0.1:5173)，API 文档见 [Swagger UI](http://127.0.0.1:8000/docs)。启动脚本安装依赖并同时启动 API、Thinker 和 Vite；Ctrl+C 停止三个进程，日志在 `data/`。当前验收为连续在线，服务重启后请在界面点“新建会话”；不承诺完整恢复。任务 SQLite 使用 WAL，默认 `data/super_xd.sqlite3`。

若依赖已安装，也可在三个终端分别运行：

```bash
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
.venv/bin/python -m backend.worker
cd frontend && npm run dev -- --host 127.0.0.1
```

密钥仅服务端读取，`.env` 与 `data/` 不提交。`ARK_API_KEY` 和 `TAVILY_API_KEY` 用于真实模型与搜索。Speech 使用独立凭证，详见 [TTS 配置](docs/TTS.md)；缺少时文字功能可用，朗读显示配置原因，不用其他音色替代。

语音交互：点击输入框旁麦克风，允许权限并等待“正在听”；说完再点一次结束并发送。火山 ASR 最终结果自动进入对话，小何朗读回复。录音中暂停朗读，取消不发送；每次最多60秒，未完成前的识别文字只预览。独立 ASR 凭证与已有项目配置文件引用见 [ASR 配置与验证](docs/ASR.md)。

## 当前范围

### 15＋85 渐进式车控（2026-10-14）

Talker 使用 Mini，固定常驻 15 个车控函数；其余 85 个由独立 `VehicleControlPlanner` 使用 Pro、关闭 thinking 规划。涉及长尾、冷热混合或有依赖的组合请求，通过 `delegate_vehicle_control(skill_ids)` 传递用户未经改写的整句、最新车况、历史及本轮真实已完成回执。整句委托与直接热车控不能在同组混用，避免两层重复执行。

Pro 只可调用 `load_skill` 和 `submit_vehicle_plan`，所选子系统的实际函数 schema 渐进加入计划的 action 数据。冷 schema 始终不进入 Talker 的可调用工具集合。计划包含原文来源、动作、依赖和保持项；actor 整组预检后并行下发独立动作，依赖和写入冲突动作仍按顺序等待回执；每项由实际模拟车辆提交回执核销。规划接受、模拟成功和命令 ACK 均不代表已执行。研究 Thinker 的建议确认流程独立保留。

15 个常驻车控与 3 个调度函数固定；另有下面 3 个地点/天气函数，因此当前 Talker 总共 21 个工具。`VEHICLE_CONTROL_MODEL` 可独立配置，默认 `doubao-seed-2-1-pro-260915`。原文覆盖和结构校验无法证明任意自然语言语义正确，多意图效果与延迟仍需真实模型验证，见[验收记录](docs/VALIDATION.md)。首字与车控的 2 秒是原型目标，新增 Pro 调用不承诺达到该时间。

### Google Places / Weather 前台 FC（2026-10-14）

简单 POI 和天气查询现在直接走 Talker → FC → 同一 Talker，不登记后台任务。固定新增 `search_places(query,max_results?)`、`get_weather(location,date)`、`start_poi_navigation(place_id)`，与现有车控及 Thinker 共存。

- “巴黎明天天气怎么样”“深圳今天天气怎么样”：Google Places 解析城市，Google Weather 查询；Google 不覆盖的地点（例如深圳）明确使用 Open-Meteo 真实数据。日期按目标城市时区计算，按 `displayDate` 匹配，避免凌晨取到前一天。支持当前天气、今天、明天及预报范围内的指定日期。
- “找巴黎卢浮宫附近的咖啡馆”→“导航去第一个”：展示真实地点、地址、Google Maps 链接；选定本会话查到的 `place_id` 后，经现有模拟车端回执设置导航状态。无当前位置时先询问区域，不假设 GPS；不计算路线、距离或到达时间，不依赖 Routes API。
- 查询异步执行，用户新输入可以打断；旧结果不能改变当前地点候选或触发导航。结果卡和工具轨迹保留来源、日期、查询时间及错误。权限/配额错误不会伪装成成功或悄悄换源。

服务端 `.env` 或启动进程环境配置 `GOOGLE_MAPS_API_KEY`，GCP 项目启用 Places API (New)、Weather API 和结算。密钥仅以请求头发送，浏览器不接收。仅放入 `.zshrc` 的密钥需由启动 shell 导出或同步到 `.env`；修改后重启 API 并新建会话。此次已将本机现有密钥同步到 `.env`。

可运行 `.venv/bin/python -m scripts.evaluate_maps_fc` 验证真实模型、真实 Google API 与模拟车辆；报告见 [maps-fc-live.json](docs/maps-fc-live.json)。该报告验证文本/工具路径，不是首音频延迟验收。网络与模型延迟仍存在，直查减少的是后台登记和轮询步骤。

- 默认 Talker：`doubao-seed-2-0-mini-260428`，thinking disabled；简单实时查询的 quick worker 同样用 Mini。天气入口（query_domain=weather）本原型仅支持 quick，非 quick 参数会在登记前拒绝，由同一 Talker 重新处理；复杂非天气任务可用 research。复杂研究 Thinker 使用 `doubao-seed-2-1-pro-260915`、thinking enabled；车控规划 Pro 独立关闭 thinking，普通热车控不经过 Pro。
- **本版不做车控目录的 embedding 或语义检索**，无向量下载、预计算、关键词替代或 `discover_tools`。保留精确 100 个 mock function 和 10 个 skill。仅 Pro 车控规划层按固定子系统 ID 加载详细 schema；Thinker 的真实 Tavily 网页搜索独立保留。
- 固定常驻 15 个热车控：空调电源、绝对/相对温度、风量、单窗位置、关全部窗、座椅加热/通风/按摩/位置、车内灯、氛围灯电源、音量、播放控制、导航目的地。另有固定 3 个调度工具 `delegate_vehicle_control` / `delegate_task` / `manage_task`。
- prompt 首段为固定规则与 skill 分类，persona、车况、近期历史和任务摘要后置；Talker schema 不因历史使用扩张。没有声称测得服务商 cache 命中率。
- 一次 Talker 调用直接选择聊天/车控/委托。热车控收到实际模拟执行结果后用 persona 模板反馈，正常成功路径无第二次 LLM。完整 tool_call / tool_result 写入本地轨迹，近期会话审计历史供上下文使用；模型上下文压缩重复状态。
- Pro 每次委托单独渐进加载能力；读取实际值后由同一 Talker 回答，执行回执完整汇总。空会话误调用任务工具会返回真实 `no_task` 错误，让同一 Talker 在原步骤上限内继续；不另加判别模型。原始错误尝试保留。
- Thinker 真实 ReAct，默认并发 2、最多 20 推理/工具步骤、从登记起总计 5 分钟硬上限（含排队、等待澄清）。外部调用期间不持有数据库事务。进度卡静默更新；澄清/最终结果等当前生成和朗读结束后播报。
- persona 预设专业/温暖/活泼，可结构化自定义。下一轮生效，当前轮固定启动版本；任务、条件和历史保留。Thinker 中性输出，回流由当前 persona 模板表达，无额外润色模型。
- 默认按说话来写：提示模型通常用1–3句、数十字，先给结论；车控一句具体回执、任务接手一句确认、澄清一个关键问题。默认朗读不念Markdown、表格、URL；结果卡与轨迹保留详情，任务可提供独立spoken_summary。简短由生成契约控制，服务端不按句数、字数或关键词删掉已返回的事实、条件或额外问句；缺口语摘要的旧报告保留完整事实，仅清理显示格式。
- 可选朗读只接火山 Speech **小何 2.0**，使用双向 WebSocket 合成 PCM，经独立音频连接逐包送到浏览器 AudioWorklet；收到音频即可播放，无需收齐整段。连接预热/复用不缓存音频；首段文本到达才启动合成session，避免长规划期间空等导致零音频。开启朗读时，第一阶段仅输出工具调用或短聊天路由；确认聊天后以 tools=[] 再次调用模型，按完整句子流式送入 TTS。这样增加一次聊天模型调用，但不播可能夹带工具调用的前缀。车控下发时播“正在为你……”；成功回执静默处理，失败补报；完成文字仍由实际回执生成。

## 模拟车机与执行回执

浏览器收到 `vehicle.command` 后调用独立模拟执行端 `POST /api/sessions/{id}/vehicle/commands/{command_id}/execute`，再提交绑定的 `vehicle.receipt`。模拟端持有独立状态，校验并实际执行或拒绝，返回 `ok/reason_code/resume_condition`。默认模拟下行200ms、执行300ms、上行200ms，总计约700ms；重复执行同一command_id返回同一回执。公开WebSocket不接受旧`vehicle.execute` ACK。驾驶环境可设置车速、档位、导航、通话和IANA时区。此执行器运行在本机服务进程，没有连接真实ECU。

下发后立即播报进行中状态，成功无需再说一次；失败才补充原因。收到回执前不会生成完成话术。界面保留实际完成结果，trace分别记录下发反馈、回执和语音文本。超时只能报告执行状态未确认。

左舵模拟车：左前/主驾=`driver`，右前/副驾=`front_passenger`，左后=`rear_left`，右后=`rear_right`。越界、错误类型、行驶中受限操作、充电口/资源约束拒绝后状态保持不变。导航只保存模拟目的地、途经点与偏好，不算真实路线、不订座、不支付。后台动作建议按最新车况校验，展示后需用户明确确认执行。

明确字面目的地命令在下发前核对地点原文，缩写、翻译或截断会被拒绝，参数不会自动改写；上下文中的地点引用仍需模型解析或澄清。最终三个 persona 的“上海虹桥站”专项均保持完整地点并实际提交，早期错执行样本继续保留为失败。

另有范围有限的字面校验：明确要求氛围灯开关保持不变时拒绝额外电源写入；五类座椅/方向盘/后视镜舒适功能、氛围灯与车内照明明确错选目标时拒绝；指定车窗保持原状时拒绝覆盖该窗的位置写入。没有出发地的字面“导航去最近机场”也不能直接登记任务。可恢复拒绝让同一 Talker 在原步骤上限内继续。结果记录原句证据与真实当前值，不改写参数、不选择替代工具，也不代表已验证所有自然语言授权。模型错误尝试、被拦截结果与真正执行回执分别保留。

新输入立即打断旧文本、合成请求和音频；不取消后台任务。迟到执行回执只记原轮次trace，不更新当前输出、朗读或车况快照；命令 ID 去重。打断历史使用worklet已渲染时长与供应商句子边界，仅保留已完整播放句子，半句不猜测。显式任务取消在步骤边界生效。任务修改递增 revision，旧条件的网络结果不能覆盖或播成新结果；event_id 去重，已终止任务不能被迟到写入复活。

## 模型输出与 Agent 轨迹

界面顶部、助手消息和任务卡均可打开“模型输出 / Agent 轨迹”，按全会话、轮次或任务回看并复制 JSON。模型消息、工具参数与完整结果、车控回执、任务版本/步骤、错误和取消分别记录；模型原文与最终 persona 模板反馈明确分开。同一轮加载 skill 后的每次模型调用有独立 call_id，迟到记录保留原轮次与状态，不成为新轮次的回答。

轨迹保存实际收到的完整内容，不用摘要替代；流式调用保留收到的原始 JSON 帧，非流式调用保留供应商响应及元数据。`content=null`、未返回的 usage 和 reasoning 均如实展示。只观察供应商实际返回的 `reasoning_content`，不推测、补写或宣称获取未返回的内部推理。trace 启用前的轮次无法补回当时未采集的供应商元字段。

Talker 通过独立队列与线程异步写日志，不让 SQLite 轨迹写入阻塞前台事件循环；写入失败会显示记录可能不完整。日志位于本机 `data/super_xd.sqlite3`，对凭证字段、已知环境密钥值与 Bearer 文本脱敏，但**用户对话、模型输出和工具结果以明文保存**。本地调试接口不提供生产级认证或日志加密，分享数据库或轨迹前应检查其中的用户数据。API 与分页约定见[接口文档](docs/INTERFACES.md)，真实记录与验证边界见[轨迹验收](docs/TRACE_VALIDATION.md)。

## 演示

1. “晚上好，陪我聊两句。”
2. “把空调温度设为23度。” → 核对温区、空调电源、回执。
3. “把左前车窗打开到一半。” → 主驾窗 50%；“关闭全部车窗。”
4. “打开后视镜加热。” → Talker 委托 Pro 规划 mirrors，actor 校验后执行；后续 Talker 工具集合不变。
5. “请调研巴黎科学工业城适合带孩子参观的活动，读取官方来源。” → 后台卡片出现，继续闲聊和车控。
6. “进度怎么样？”；多个任务时“把那个取消”需消歧。任务卡可直接查看/补充/回复/取消，显示 ID 与版本。
7. 调研中切换 persona，追加“只考虑周一开放的场馆”；结果应保留条件并用当前风格回流。点击朗读前须配置 Speech 专用密钥。
8. 打开“模型输出 / Agent 轨迹”，核对工具调用模型原文、执行回执及最终模板，或从任务卡查看 Thinker 步骤与 revision。

## 验证与文件

15＋85架构的完整24轮先为14/24，修复后第二批22/24；剩余两项修复后的受影响8轮为8/8，三批失败与原始尝试分别保留，不能拼成完整24/24。最终本地1364项、前端38项与构建通过。第二批7轮纯热车控完整反馈1035–1694ms，Pro路径5649–25111ms；新增真实ASR→Pro车控→小何PCM单轮通过，首非零PCM6159.5ms，未达2秒，未测实体麦克风。当前仍可能有Mini漏委托或自由文本假完成，不能宣称任意多意图可靠。详情、人工审计和此前69轮/天气/语音历史见[当前交互与语音验证](docs/VALIDATION.md)。

```bash
.venv/bin/python -m pytest -q
(cd frontend && node --test tests/*.test.mjs && npm run build)
# 下列是有费用/网络的真实调用，运行前启动服务
.venv/bin/python -m scripts.evaluate_progressive_control --run --base http://127.0.0.1:8000 --output docs/progressive-control-new-run.json
.venv/bin/python -m scripts.evaluate_progressive_voice --run --base http://127.0.0.1:8000 --output docs/progressive-voice-new-run.json
# 既有适配器/旧专项诊断单独使用，不合并为本版验收
.venv/bin/python -m scripts.probe_apis
.venv/bin/python -m scripts.benchmark --pairs 10
.venv/bin/python -m scripts.evaluate_personas
.venv/bin/python -m scripts.probe_navigation
# 当前默认1500ms，仅离线聚合，不发请求
.venv/bin/python -m scripts.audio_benchmark --input browser-audio.json --manifest audio-manifest.json --output docs/audio-latency-new-run.json
# 历史2秒口径须显式--target-ms 2000，并输出新文件
```

离线测试用明确注入的 fake 网络客户端做故障/竞争验证；运行原型始终使用真实客户端。**1.5秒目标针对每次 query 提交至浏览器首个真实非零 PCM，并非只要求 P50。** 报告 P50/P95、全体提交的 ≤1500ms比例和逐次失败；任一次超标、失败或缺少音频都不能宣称全请求达标。空帧、静音、占位语音与缓存声音不计有效响应。历史2秒报告原样保留。

当前1364项后端与38项前端回归、构建通过；Pro规划在网页明确标注来源和真实回执，重连可恢复。此前10热工具版本的1204项、前端采集/识别/播放30项，以及更早ASR接入626项/ASR专项67项均为历史规模。真实中文识别与运行服务代理链路见[ASR验证](docs/ASR.md)，实体麦克风尚未实测。旧音频、导航、人设和轨迹专项分别保留，不与本版24轮车控验收合并。

真实人设回归共 18 轮，另有 3 次真实车控预置，全部通过。专业、温暖、活泼使用相同初始状态和历史：车控工具、参数与路径一致，表达不同；普通寒暄没有触发车控或委托。失败的早期运行也保留在报告目录中。该回归不替代正式延迟验收。

- [实测与限制](docs/VALIDATION.md)
- [浏览器端到端 PCM 音频验收](docs/AUDIO_VALIDATION.md)
- [100 函数与 10 skill 目录](docs/TOOLS.md)
- [Thinker harness 与真实闭环](docs/THINKER.md)
- [火山 Speech 配置与凭证要求](docs/TTS.md)
- [火山 ASR 配置与真实验证](docs/ASR.md)
- [WebSocket / 类型接口](docs/INTERFACES.md)
- [浏览器实际操作记录](frontend/UI_VALIDATION.md)
- [真实跨人设回归结果](docs/persona-live.json)
- [导航原文保真专项结果](docs/navigation-live.json)
- [并发、澄清与音频审查及修复验证](docs/REVIEW.md)
- [模型输出与 Agent 轨迹验收](docs/TRACE_VALIDATION.md)

核心代码：`backend/session.py`（actor）、`backend/vehicle_planner.py`（Pro车控规划/严格验证）、`backend/models.py`（Ark/Tavily）、`backend/worker.py`（独立 Thinker）、`backend/db.py`（WAL 账本）、`backend/catalog.py`（严格模拟执行器）、`backend/persona.py`、`backend/tts.py` / `backend/speech_stream.py`、`frontend/src/StreamingSpeech.ts` / `frontend/public/pcm-player-worklet.js`。


## 2026-10-14 工程修复验证

Thinker的300秒预算只累计`running`时间；澄清默认最多等1800秒（`THINKER_CLARIFICATION_TIMEOUT_SECONDS`）。reply/update恢复上一次完整工具消息组和来源，追加新条件，保留累计步骤；只有goal改变才清空上下文。既有来源不自动证明新条件，快速任务需模型明确核实可复用证据。任务时间及事件仍在同一事务更新。

Planner未覆盖原句字符改为warning，准确来源绑定、schema、能力、依赖和keep冲突仍严格校验；未新增正则或语义文本守卫。时区来自车辆设置，缺省UTC，可在界面切换为设备时区。全双工麦克风/VAD不在本轮范围内。

离线回归、专项用例及真实Ark/Speech时序记录见[工程验证数据](docs/engineering-validation.json)与[真实调用轨迹](docs/engineering-live.json)。真实服务首PCM与浏览器首渲染分别统计，不把700ms模拟回执耗时当成整个语音延迟。

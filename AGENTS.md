# AGENTS.md

本仓库是车载语音助手的 Talker–Thinker 双层架构原型（Super XD 语音座舱实验室），车辆全部为模拟。修改代码前请完整阅读本文件。

## 0. 本文件的编辑权限

**只有人类维护者可以编辑本文件。** AI agent 不得以任何方式修改、重写、删除或新建本文件，包括格式调整和"顺手修正"。

如果你认为本文件有错误、过时或缺失的内容，请在任务回复或 PR 描述中写明建议，交由人类决定。任务说明与本文件冲突时，请先指出冲突再继续。

## 1. 项目概览

- **Talker（前台）**：`backend/session.py` 中的 `SessionActor`，每个会话一个。负责听、说、打断、直接回答、热车控、同步查询，以及把复杂需求委托出去。
- **Vehicle Planner（车控规划层）**：`backend/vehicle_planner.py`。复合车控和非热车控由它生成结构化计划，actor 按真实回执逐项执行。
- **Thinker（后台）**：`backend/worker.py` 中的 `ThinkerWorker`，独立进程。通过 SQLite 任务表领取任务，执行 ReAct 循环，结果以事件形式回流给 actor。
- **前端**：`src/`，React + TypeScript。既是调试台，也充当模拟车机。

### 目录

| 路径 | 职责 |
|---|---|
| `backend/app.py` | FastAPI 入口：HTTP、`/ws/{id}` 控制通道、`/ws/{id}/audio` 音频下行、`/ws/{id}/asr` 识别上行 |
| `backend/session.py` | Talker 会话 actor、Talker prompt、工具分发、回执话术、主动播报 |
| `backend/vehicle_planner.py` | 车控计划的生成与结构校验 |
| `backend/catalog.py` | 100 个模拟车控函数（10 个 skill）、车辆初始状态、`execute_tool` |
| `backend/worker.py` | Thinker 进程：领取任务、ReAct 步骤、澄清、完成 |
| `backend/db.py` | SQLite（WAL）：tasks / inbox / events / trace_events |
| `backend/models.py` | 火山方舟 LLM 客户端（OpenAI 兼容）、Tavily 客户端 |
| `backend/asr.py`、`asr_endpoint.py` | 火山流式 ASR |
| `backend/tts.py`、`speech_stream.py` | 火山双向流式 TTS（PCM 24kHz） |
| `backend/persona.py` | 人设配置与回执话术模板 |
| `backend/speech_text.py` | 送 TTS 之前的展示语法清理 |
| `backend/google_maps.py`、`maps_tools.py`、`weather.py` | POI 搜索、天气（Google，回退 Open-Meteo） |
| `src/App.tsx` | 主界面、控制通道消息处理、模拟车机 |
| `src/StreamingSpeech.ts` | 音频播放、播放状态与指标上报 |
| `src/StreamingRecognition.ts` | 麦克风采集与 ASR 流程 |

后端读取的环境变量（见 `settings.py`、`models.py`、`worker.py`）：`ARK_API_KEY`、`ARK_BASE_URL`、`TALKER_MODEL`、`THINKER_MODEL`、`VEHICLE_CONTROL_MODEL`、`DATABASE_PATH`、`MODEL_TIMEOUT_SECONDS`、`TOOL_TIMEOUT_SECONDS`、`TAVILY_API_KEY`、`GOOGLE_MAPS_API_KEY`、`THINKER_CONCURRENCY`、`THINKER_MAX_STEPS`、`THINKER_TIMEOUT_SECONDS`。

## 2. 外部约束（不可改变）

- LLM、ASR、TTS 全部使用火山引擎的托管服务。**不能微调，也不能改变它们的推理速度。**
- 因此，行为改进只能来自三个地方：prompt、few-shot 示例、结构化输出与结构化校验。延迟改进只能来自我们自己的管线：流式化、并行化、减少模型调用次数、提高缓存命中。
- 不要提出或实现依赖微调、自训练模型、自定义 tokenizer 或约束解码的方案。
- 缺少凭证时直接报错。不得生成假回复或静默使用替代服务。

## 3. 硬规则：禁止正则

**不允许使用 `re` 模块，也不允许用任何其他正则引擎。** 新代码一律不得引入。现有代码中的正则属于待清理项，不得作为新代码的参照。

- **禁止用正则理解语义**：不要通过匹配用户原话、ASR 文本或模型输出文本来判断意图、否定、保持、授权、目标对象、任务指代或数字是否正确。这类判断只能交给模型，代码只能在结构化字段上做校验。
- **禁止针对单个用例打补丁**：某个用例失败时，不得在代码里加入针对那句话的判断，也不得往 prompt 里追加只服务于那一个用例的规则。
- **格式处理换成非正则写法**：

| 用途 | 替代方式 |
|---|---|
| ID、请求号校验 | `str.isalnum()` 加字符集合判断、`uuid.UUID()` |
| 日期 | `datetime.date.fromisoformat()` |
| URL、域名 | `urllib.parse.urlparse`，配合 `str.split('.')` 逐段校验 |
| 密钥脱敏 | 按已知密钥做 `str.replace`，按字段名过滤字典 |
| 空白归一 | `' '.join(text.split())` |
| Markdown 转纯文本（送 TTS 前） | `mistune` 或 `markdown-it-py` 渲染为纯文本 |

- JSON Schema 中的 `pattern` 字段只允许用于工具参数的纯格式约束（如 `YYYY-MM-DD`），不得用来表达语义。

## 4. 架构不变量（改动不得破坏）

### 会话 actor
1. **单写者**：会话状态（`self.vehicle`、`self.turns`、`self.current` 等）只在 `SessionActor.handle()` 中修改。网络回调、模型流、TTS、定时器一律 `post()` 事件回队列，不得直接改状态。
2. **actor 事件循环里不做阻塞 IO**：新增的 SQLite 或其他同步调用使用 `asyncio.to_thread`。
3. **过期结果一律丢弃**：任何迟到的模型结果、回执或音频，都要先用 `is_current(turn)` / `audio_valid(turn)` 检查。被打断轮次的结果只能写进 trace，不能进入用户可见的输出或 TTS。

### 诚实性
4. **说的和做的分开**：车控完成话术只能由真实回执生成（`receipt_detail` 加人设模板）。模型文本不得声称"已完成""已开始"。任务接手话术只能来自任务登记结果。
5. **车端是执行权威**：车控动作的最终成功或失败以车端回执为准。模拟、预检只用于提前拒绝，不能当作回执。
6. **人设只改说法**：同一输入在不同人设下，工具调用序列和参数必须完全一致。人设不能影响路由、工具选择、参数和权限。
7. **Thinker 只输出中性内容**：Thinker 不知道人设存在，只产出 `summary` 和 `spoken_summary`，由 Talker 侧负责表达。替用户写的内容（邮件、消息）用用户本人的口吻。

### 异步任务
8. **状态和事件在同一事务内写入**（`db.py` 的 `_event` 与状态更新同事务）。不得拆成两步。
9. **revision 语义**：用户的 reply/update 会让 revision 加一。worker 在步骤边界检查 inbox 和 revision，旧 revision 的结果只记 trace，不得写入任务。
10. **有副作用的工具必须带幂等键**（`task_id + step` 或 `task_id + step_id`）。
11. **不可逆动作先确认**：发送、下单、支付类工具接入时，必须先进入 `awaiting_confirm` 状态，由用户明确确认，确认 token 绑定内容哈希。"嗯"这类附和不算确认。

### 工具边界
12. Talker 的工具集固定为热车控 + `delegate_vehicle_control` / `delegate_task` / `manage_task` + 地图工具（`tools_for`），不随对话增长。非热车控只能经由 Vehicle Planner。
13. 新增车控函数统一走 `catalog.register`，并同步更新 `EXPECTED_COUNTS` 断言。

## 5. Prompt 规范

- prompt 位置：Talker 在 `session.py` 的 `CORE_PROMPT` 和 `messages_for`；人设在 `persona.style_prompt`；Thinker 在 `worker.py` 的 `SYSTEM_PROMPT` / `QUICK_PROMPT`；Planner 在 `vehicle_planner.PROMPT`。
- **排列顺序：稳定的在前，动态的在后。** 固定规则 → 人设 → 历史 → 会话快照（车况、任务、`current_utc`）→ 本轮输入。动态内容不要插在历史前面，否则缓存命不中。
- 修复错误优先补 **few-shot 示例**，其次是一句通用规则。规则必须能覆盖一类情况，不能只写给某一句话。
- 不要在每轮额外注入"再次确认原句"之类的补丁消息。需要模型逐项处理的，改用结构化输出（例如 planner 的 intent 账本）。
- 时区和地点不得写死，从车辆或用户设置中读取。

## 6. 修复问题的工作流

1. 先把失败场景写成评测用例（输入、车况、期望的工具调用或期望行为），放在 `evals/` 目录下。
2. 只用 prompt、few-shot 或结构化字段来修复。不得匹配文本。
3. 跑完整评测，在 PR 描述里写明修改前后的通过率，以及新增失败的用例。
4. 用 trace 抽查至少一条相关轨迹：模型输入、工具调用、回执、最终话术是否一致。

**完成标准**：评测无新增失败，或者新增的失败已在 PR 中说明理由；没有新增正则；第 4 节的不变量都保持成立。

## 7. 其他约定

- 全面使用 asyncio；新依赖优先选维护活跃、星数高的成熟库。
- trace 必须完整记录模型输入输出、工具调用和回执，并且不得包含密钥（走现有脱敏路径）。
- 代码注释写"为什么"，不要写长篇的辩护式说明。
- 不要提交 `data/` 下的 SQLite 文件、`.env` 和 `__pycache__/`。

# AGENTS.md

修改前完整阅读。改动须符合实际架构，全文不超过4000个字符。

## 职责与入口

本项目是车载语音助手的 NLP Agent Harness。Gateway负责鉴权、会话/上下文持久化、任务账本/租约、设备连接和工具交接；外部团队执行设备动作。本仓库不实现生产Gateway、Redis或设备执行器。

Python包在`src/super_xd/`，必须安装后导入；禁止恢复旧backend兼容壳或给sys.path补src。
- `nlp/api/app.py`是生产HTTP/NDJSON入口，`nlp/assembly.py`冻结配置并装配资源。
- `nlp/contracts/`只含数据和校验；`catalog/`只含工具/技能契约。描述文案由`prompts/catalog.py`编译。Schema从契约导出，不手改副本。
- `nlp/engine/`管理运行、预算、取消、checkpoint与证据进展；不得依赖业务策略、编排或具体适配器。
- `nlp/orchestration/`组合Talker、前台Reasoner、后台tasks；`policy/`校验来源、权限、参数和真实回执，不做IO。
- 模型只用`ModelRequest`→`ModelResult/ModelEvent`；`ModelService`按冻结角色绑定。`nlp/adapters/`实现模型、检索、Gateway传输；`prompts/`负责加载、编译和表达；`release.py`指纹只覆盖生产源码与资源。
- `demo/`拥有api、session、SQLite tasks、audio、resources与devices；调用同一个NLPService。生产nlp不得导入demo或其依赖。
- 根`prompts/`、`config/`是资源源文件，构建时装入wheel；`nlp/resources.py`统一定位。Demo可写数据使用`SUPER_XD_WORKSPACE`，不写安装资源。
- 测试分`tests/nlp`、`tests/demo`、`tests/evals`，共享fake/builders放`tests/support`，禁止测试文件互相充当库。
- 契约见`docs/GATEWAY_CONTRACT.md`，架构见`docs/NLP_HARNESS.md`。替代实现、兼容壳和过时文档应删除并清引用。

## 必须保持的不变量

1. 模型理解语义；代码只做结构化校验、来源核对和状态转换。禁止正则引擎、`re`和针对某句话的关键词补丁。格式处理复用`nlp/textutil.py`；Markdown朗读用`markdown-it-py`。JSON Schema的`pattern`仅可约束纯格式。
2. 工具调用不等于成功。预告只能用进行时/将来时，完成以真实回执或任务登记确认响应为准。模拟/预检不能生成执行成功。结果`unknown`必须等待核对，不能自动重发动作。
3. 每次运行的身份、context revision、release/model/semantic policy版本不可改写。迟到结果不能进入新轮输出；新上下文启动新run。checkpoint v2只保存一次start及增量，恢复重建并校验前缀；只接受Gateway可信存储回传，不面向终端。输入/结果/checkpoint的容量限制在契约中显式拒绝，不截断假装成功。
4. Gateway保证每个checkpoint只有一个推进者，原子保存checkpoint与待交接请求；工具执行保留稳定幂等键。任务状态与事件原子写入，写入核对revision及执行租约。reply/update递增revision，旧结果只留审计。只读任务的前后台切换仅改delivery_mode，同task/revision/lease/checkpoint/预算继续；当前不做跨worker租约移交。
5. 取消停止后续推理和交接，不宣称撤销已下发动作。模型/工具/时间预算跨恢复累计；断线关闭上游流。异步任务边界检查取消、修订、澄清和期限。生产TaskService及Demo Worker执行前拒绝缺失/不匹配的登记版本，不能补写旧任务身份。
6. 媒体/导航串行依赖必须依据实际回执重新准备，不能把expected状态当事实。query/control与cloud/device是独立维度；能力缺失明确失败。内容ID须有真实来源，模型不能编造播放URL。暂停、停止、退出不可混同；主动暂停后不自动恢复。
7. Talker固定26工具；非热车控走前台Reasoner。车控目录变动同步`catalog.register`与计数断言。地点参数必须有来源；POI导航只能引用以前轮次已展示的结果。车书资料不授予执行权限。
8. 人设只改表达，不能改变工具序列、参数、权限；Talker一次调用同时决定与表达。后台Reasoner不读取人设，提供中性结果，由Talker表达。替用户撰写的内容采用用户口吻。
9. 发送、下单、支付等不可逆工具接入前必须有明确确认，确认令牌绑定内容哈希，不能把附和当确认。
10. Demo会话保持actor单写者：状态只在`SessionActor.handle()`内改，异步回调post事件；迟到模型/回执/音频先检查轮次。同步数据库IO通过`asyncio.to_thread`。通知按版本/期限/注意力仲裁；Registry回收闲置会话不删除账本。

## Prompt与配置

行为模板采用专家的`common/talker/talker_event/reasoner/reasoner_foreground/reasoner_background/persona/context.md`组织，优先保留专家原文与叙述风格，只适配实际能力与协议。车控计划另用`vehicle_planner.md`，短运行协议用`runtime.md`；不得搬回重复禁令长清单。工具说明保留TOML，少量原生示例在`talker/examples.json`；固定播报在`speech/`，embedding指令在`manual/`。

启动时严格校验文件、section、key和变量并缓存；请求路径不得读文件。文字配置不定义类型、参数范围、权限或执行逻辑。稳定规则→选中人设→原生示例→历史→动态快照→本轮输入；时间、音区、车况、任务、检索结果仍作为运行时数据。时区和地点从事实读取。

文件合并不等于token或延迟下降。减少重复规则、示例和调用次数须有测量；不为修行为增加常规串行模型调用。能力投影按角色固定接口压缩，不能按关键词隐藏工具。检索按来源版本和内容判重、限制无进展循环。优先通用few-shot/结构化输出，不能把评测原句抄进示例。schema保持简单、参数由代码精确校验。

地区预设在`config/regions.toml`，`SERVICE_REGION=europe/china`在启动时绑定LLM、ASR、TTS；本机巴黎选欧洲，国内选火山。不按IP猜地区，不在请求中跨地区降级；未选地区时使用显式服务绑定。缺配置/凭证直接报错，不伪造回复。不得引入微调、自训练、自定义tokenizer或依赖改变外部推理速度的方案。

## 验证与交付

- 依赖以`pyproject.toml`声明、`uv.lock`锁定；核心`uv sync --locked --no-dev`，本地`--extra demo`。生产配置在装配时冻结，不隐式加载.env。
- 离线：`.venv/bin/python -m pytest -q`；边界：`python -m scripts.check_import_boundaries --smoke-startup`。配置：`.venv/bin/python -m scripts.validate_prompt_config`和`-m scripts.validate_service_config`。
- 行为变更先补泛化评测，至少重复3次：`.venv/bin/python -m evals.harness --repeat 3 --out data/evals/<name>.json`。评测直接驱动生产NLPService，设备独立执行；报告通过率、新增失败、首句P50/P90及平均模型调用次数；人设对比保留语义参数。
- 话术变化跑`evals.speech_review`作辅助，并抽查模型输入、工具、回执、输出完整trace。供应商拒绝访问时明确说明，不能把离线通过当作真实模型质量、延迟或真车验证通过。
- Talker主延迟是输入提交到TTS首帧PCM，另记首个非零帧和浏览器实收；首文字、送入TTS和最终答案不是首帧。ASR/VAD、TTS自身耗时及答复完成单独统计。
- 延迟验收区分零缓存与命中、密集调用与停顿交互；保留超时/取消及逐次网络尝试，重试共用累计预算，不能只报最后成功请求或成功样本耗时。
- prompt/config改动后重启相关服务；embedding指令或模型变化须重建车书索引。页码/原文/页图保留真实来源，余弦分数不是可信度。
- trace完整但脱敏：密钥、令牌、媒体播放能力URL不能泄露。不要提交`.env`、`data/`、数据库或`__pycache__`。注释说明原因，避免重复实现与长篇辩护。

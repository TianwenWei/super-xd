# 专家模板与实际运行协议

本轮采用外部专家 `Talker–Reasoner 提示词框架 v0.2` 的八模板组织，保留“所处系统—用户处境—怎样决定—怎样做事—怎样交付”的叙述。原材料来自 `talker_reasoner_prompts/prompts/*.txt`；当前文件是与本仓库真实工具、权限和交付协议对齐后的版本。

| 文件 | 用途与组装 |
| --- | --- |
| [common.md](common.md) | 共享的用户处境、数据边界、诚实与安全；各角色固定前缀 |
| [talker.md](talker.md) | 用户轮即时回应、直接工具和前后台委托 |
| [talker_event.md](talker_event.md) | 事件表达契约，禁止新增业务工具操作；当前事件交付走确定性播报，未增加事件模型调用 |
| [reasoner.md](reasoner.md) | 只读研究的目标、查询、证据和交付 |
| [reasoner_foreground.md](reasoner_foreground.md) | 用户等待时尽快交付；等待期限由运行时管理 |
| [reasoner_background.md](reasoner_background.md) | 异步补充条件和中性结果，不预设播报时刻 |
| [persona.md](persona.md) | 保留专家原文；只注入选中的人设 JSON，不再追加重复人设段 |
| [context.md](context.md) | 注入当前 Gateway 快照，包含当前音区、能力、地点、时间、任务与播放事实 |

Talker 组装顺序为 `common → talker → 完整冷车控目录 → persona → 原生示例 → 历史 → context → 本轮输入`。固定规则、工具和示例可以缓存；实时事实留在当前输入前，历史不会覆盖本轮音区。能力投影不按用户关键词筛选，Talker 仍固定 26 工具。

研究请求为 `common → reasoner → query_mode 对应的查询说明 → delivery_mode 对应的交付说明 → 任务快照与过程`。前后台使用同一个任务与累计预算；两者都没有车辆执行、发送或支付权限。查询方式 `quick/research` 与交付方式 `foreground/background` 是独立维度。

[vehicle_planner.md](vehicle_planner.md) 是实际车控计划协议：只提交 `submit_vehicle_plan`，不宣称执行完成。[runtime.md](runtime.md) 仅保存短的上下文归属、能力数据包装、重试、修订和收尾消息。它们保留严格命名 section，避免把运行时协议与专家角色叙述混杂。

## 与专家原稿的必要差异

- 专家的 `delegate_to_reasoner` 分别映射为现有 `delegate_vehicle_control` 与 `delegate_task(mode=...)`。整组车控的计划校验和回执链保留；没有让研究器直接执行车辆动作。
- `ask_user/finish` 映射为实际的 `ask_clarification/finish_task`。澄清通过暂停任务、接收后续回答继续，不声称模型调用会同步收到用户回答。
- 当前未接入 `screen_tap`、`confirm_with_user`、`call_agent`、`schedule_action` 等工具，已删除相关调用示例和能力承诺。不可逆能力不能靠提示词模拟开通。
- 前台研究的 `spoken_summary` 由运行时直接交付；研究器输出保持中性，不读取人设，以便同一任务安全转后台。没有额外的 Talker 润色调用。
- 专家 XML 上下文中若干字段尚无生产契约。这里保留实际的版本化 JSON 快照，不虚构屏幕、记忆或第三方 Agent。生成历史完整保留，实际播放状态独立传入。
- 专家的“一秒左右”是设计目标，未写成系统已经满足的承诺；验收以实际 TTS 首帧 PCM 为准。

## 工具和资源

工具用法放在所属 TOML，参数类型、范围和权限仍在 Python 契约中。原生 [talker/examples.json](talker/examples.json) 只展示少量调用协议，使用真实工具名与 schema；示例数据不是当前用户事实，不抄评测原句。

固定播报保留在 `speech/`；车书 embedding 指令保留在 `manual/`。本轮未改变 embedding 指令或模型，不需要因此重建索引。

所有文件、命名 section、文字 key、变量及示例工具协议在启动时严格校验并缓存。请求只处理内存中的已编译模板；资源清单由 `PROMPT_RESOURCE_FILES` 声明，新增、遗留或漏装文件都会导致验证失败。

## 本轮文本测量

同一代码口径、紧凑 JSON、Unicode 字符数：

| 项目 | 原版 | 专家模板版 |
| --- | ---: | ---: |
| Talker 固定核心，含完整冷车控目录 | 11,026 | 7,947 |
| 原生示例消息数 | 104 | 31 |
| 示例工具调用数 | 34 | 9 |
| 原生示例序列化字符数 | 14,591 | 3,490 |

这些是字符与消息数量，不是供应商 token，也不代表实际延迟下降。共享模板使某些研究/规划请求的文字增加，需要与行为通过率、模型调用次数和真实首帧数据一起评估。

验证入口：`.venv/bin/python -m scripts.validate_prompt_config`；相关离线测试在 `tests/nlp/test_role_prompts.py` 和 `test_prompt_templates.py`。本次新增泛化回归集为 `evals/cases/expert_prompt_contracts.json`，真实行为验收至少重复三次。

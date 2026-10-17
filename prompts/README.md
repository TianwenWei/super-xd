# Prompt 配置

模型主提示词只有三个角色入口。每个文件按 `## [section: 名称]` 分段；运行时选择当前需要的段，不把整份文件发给模型。

| 入口 | 职责 | 每次如何选择 |
|---|---|---|
| [talker.md](talker.md) | 前台对话、工具决策、表达风格、上下文包装 | 固定 `system`；当前一个人设；历史；动态快照与本轮输入 |
| [reasoner_foreground.md](reasoner_foreground.md) | 前台复杂车控计划，只规划、不执行 | `system`；仅提交协议不符时使用 `retry` |
| [reasoner_background.md](reasoner_background.md) | 后台快速核验、多步研究与结果交付 | `common` 加 `quick` 或 `research`；按运行阶段使用其余段 |

工具 schema、预算、权限、执行校验与检查点协议由 Python 代码负责；Prompt 不能放宽这些边界。

## 段与兼容接口

`backend/prompt_templates.py` 是 section、变量与组合关系的严格契约。原有 `load_prompt(name)` / `render_prompt(name, **variables)` 名字继续使用，旧碎片文件已经删除，不存在两份提示词来源。

| 文件内 section | Python 模板名 | 变量 / 时机 |
|---|---|---|
| Talker `system` | `talker` | `hot_tool_names`、`skill_index`；固定规则 |
| Talker `persona` | `persona` | `persona_json`；表达边界和当前 PersonaConfig 数据 |
| Talker `persona_professional` / `persona_warm` / `persona_playful` | 同名 | 只选当前预设段；自定义人设保留自己的完整配置，不隐式套其他预设 |
| Talker `history_attribution` | 同名 | `attribution_json`；主动播报的来源标注 |
| Talker `session_snapshot` | 同名 | `snapshot_json`、`speaker_name`；动态事实放在历史之后 |
| Talker `speaker_context` | 同名 | `speaker_json`；由需要音区数据的编译器复用，数据不授予权限 |
| Talker `current_turn` / `task_snapshot_notice` / `prewarm` | 同名 | 分别用于本轮边界、任务车况快照说明、预热；不混入其他请求 |
| Foreground `system` / `retry` | `vehicle_planner` / `planner_retry` | 无变量 |
| Background `common` + `quick` | `thinker_quick` | 快速核验，不包含研究模式或车辆建议工具说明 |
| Background `common` + `research` | `thinker_research` | `skill_guide`；多步研究 |
| Background `finalize` / `revision` / `retry` / `source_selection` | `thinker_` 加段名 | 只在收尾、条件更新、提交重试或来源选择时使用 |

文件首行标题和全部 section 名须与代码一致；缺段、多段、重复段、空段、段外正文、占位变量增删都会在启动时报错。`{{变量}}` 只做一次替换，不递归解释变量值。更改文件后需重启进程；没有请求时读文件或热重载路径。

Talker 的消息顺序保持：

```text
固定规则 → 当前人设 → 原生示例 → 历史 → 会话快照 → 本轮提示与用户输入
```

地点、时间、音区与任务状态不会进入固定规则前缀。人设只改变表达，工具选择与参数必须仍由跨人设评测约束。后台没有人设段。

## 独立保留的配置

- [talker/examples.json](talker/examples.json)：原生 `user / assistant tool_calls / tool` 示例。维护成对调用与回执；启动时校验工具名、参数 schema 和消息协议。
- `talker/tools.toml`、`planner/tools.toml`、`thinker/tools.toml`、`cockpit/tools.toml`、`vehicle/functions.toml`：工具与参数的文字说明；类型、范围、枚举、路由和权限仍在代码。
- `speech/*.toml`、`cockpit/feedback.toml`：固定回执、标签、播报和人设配置。`speech/persona.toml` 仍提供界面与固定回执使用的 PersonaConfig 预设；三个模型风格段位于 Talker 主文件。
- `manual/embedding_document.md`、`manual/embedding_query.md`：独立向量任务指令。修改后必须重建车书索引，与对话角色无关。

没有删除原生示例。Talker 主规则仅移除五组已被原生示例覆盖的箭头示例（热工具组合与保持、非热工具整体委托、天气、网页实时事实、情绪回应）；纠正、历史指代、状态查询、导航与任务续接等其余示例保留。快照里的对象优先级长段与固定规则重复，改为简短提醒。后台共用证据、来源、条件变更和交付契约，预算时机以运行时为准，不再写死一分钟。Talker 与 Foreground 的定位、路线、车控和车书来源规则以本轮能力及事实为准，不再写死模拟车、无 GPS 或某一本手册；Demo 的模拟模式仍须明确标识。

## 本地验证

```sh
.venv/bin/python -m scripts.validate_prompt_config
.venv/bin/python -m pytest -q tests/test_prompt_templates.py tests/test_role_prompts.py tests/test_prompt_config.py tests/test_persona.py tests/test_structured_prompt_capture.py
.venv/bin/python -m scripts.validate_structured_prompts capture --output data/evals/prompt-after.json
.venv/bin/python -m scripts.validate_structured_prompts compare --before data/evals/prompt-before.json --after data/evals/prompt-after.json --output data/evals/prompt-comparison.json
```

`validate_prompt_config` 输出段数、模板字符数和原生示例消息/调用数。`capture` 和 `compare` 完全离线；比较完整请求的消息数、内容字符、序列化字符、模型配置、工具 schema、非 system 消息以及完整原生示例。输出使用新文件名，避免覆盖证据。

比较旧版本时，在每个 checkout 的独立 Python 进程里 capture；模型配置、默认地点/音区/时区和 suite 的固定时间须相同。原 `--source` 混合模块导入入口已移除。需要排除本地环境文件影响时设置 `PYTHON_DOTENV_DISABLED=1`，并显式设置相同的非敏感模型及环境参数。

字符减少和离线契约测试不证明模型行为变好，也不等同于提供商 token 数或延迟。Prompt 内容变化仍需按行为评测检查工具与参数、禁止动作、跨人设一致性及真实调用延迟；离线验证不会擅自调用付费模型。发布清单覆盖三份入口、示例、文字配置与编译源码，版本变化使旧检查点失效，迁移须由明确协议处理。

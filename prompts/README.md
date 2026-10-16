# Prompt 与固定话术配置

想改模型怎么理解需求，先找 `talker/`、`planner/`、`thinker/` 或工具说明；想改用户听到的固定句子，找 `speech/`。本目录集中保存提示词、原生示例、工具说明和固定话术，执行逻辑仍在 `backend/`。

本次迁移的对照、评测与部署验证见 [验证记录](../docs/PROMPT_CONFIG_MIGRATION.md)。

常用入口：

| 想修改什么 | 文件或配置项 |
|---|---|
| Talker 的回答、工具选择和车控规则 | [talker/system.md](talker/system.md)、[talker/examples.json](talker/examples.json) |
| 委托后台任务的说明与参数说明 | [talker/tools.toml](talker/tools.toml) 中 `delegate_task` |
| 后台任务研究方式、简短结果和收尾规则 | [thinker/research.md](thinker/research.md)、[thinker/quick.md](thinker/quick.md)、[thinker/finalize.md](thinker/finalize.md) |
| 某个车控函数、参数或 skill 的模型说明 | [vehicle/functions.toml](vehicle/functions.toml) 中 `functions.<函数名>`、`skills.<skill_id>` |
| 成功回执，例如温度和阅读灯 | [speech/receipts.toml](speech/receipts.toml) 中 `set_temperature`、`set_reading_light` |
| “正在为你……”、后台进度、查询失败或地点列表 | [speech/feedback.toml](speech/feedback.toml) 中 `dispatch`、`task`、`errors`、`places` |
| 人设名字、语气和固定开头 | [speech/persona.toml](speech/persona.toml) |
| 后台结果的来源、限制说明和固定天气播报 | [speech/thinker.toml](speech/thinker.toml) |
| 模拟车端的拒绝原因和状态说明 | [speech/vehicle.toml](speech/vehicle.toml) |
| 音乐、视频、电台与导航的模型工具说明 | [cockpit/tools.toml](cockpit/tools.toml) |
| 播放器、导航与路线查询的固定反馈 | [cockpit/feedback.toml](cockpit/feedback.toml) |

## 文件索引

下面的变量名是代码提供的固定接口。`无` 表示该文件没有 `{{变量}}` 占位符。

### Talker

| 文件 | 使用时机 | 可用变量 |
|---|---|---|
| [talker/system.md](talker/system.md) | 每次 Talker 请求的第一条固定 system message，规定路由、车控、查询和任务行为 | `{{hot_tool_names}}`：代码生成的热车控函数名；`{{skill_index}}`：skill 名称与说明索引 |
| [talker/persona.md](talker/persona.md) | 固定规则之后，声明人设只影响表达 | `{{persona_json}}`：当前人设设置 JSON |
| [talker/examples.json](talker/examples.json) | 人设之后、真实历史之前的原生 `user / assistant tool_calls / tool` 示例 | 无；直接作为消息列表使用 |
| [talker/history_attribution.md](talker/history_attribution.md) | 将主动播报的后台结果放回历史时，说明该消息来自哪个任务 | `{{attribution_json}}`：来源任务信息 JSON |
| [talker/snapshot.md](talker/snapshot.md) | 历史之后、本轮输入之前的动态 system message | `{{speaker_name}}`：本轮用户座位名称；`{{snapshot_json}}`：车况、任务、音区、地点、时间等会话快照 JSON |
| [talker/current_turn.md](talker/current_turn.md) | 会话快照之后，标明接下来是本轮请求和工具结果，说明打断播放等元数据的含义 | 无 |
| [talker/speaker.md](talker/speaker.md) | Vehicle Planner 和 Thinker 使用的动态音区 system message；Talker 自身音区数据放在会话快照中 | `{{speaker_json}}`：本轮座位和能力范围内的默认目标 JSON |
| [talker/task_snapshot_notice.md](talker/task_snapshot_notice.md) | 登记后台任务时附带的说明，提醒任务上下文中的车况可能已变化 | 无 |
| [talker/prewarm.md](talker/prewarm.md) | 方舟客户端预热请求 | 无 |
| [talker/tools.toml](talker/tools.toml) | 构造 Talker 的委托、任务管理、地图/天气、网页与车书工具说明 | 无；工具及参数说明文字 |

### Vehicle Planner

| 文件 | 使用时机 | 可用变量 |
|---|---|---|
| [planner/system.md](planner/system.md) | 复合车控或非热车控规划请求的固定 system message | 无 |
| [planner/retry.md](planner/retry.md) | Planner 没有通过 `submit_vehicle_plan` 提交结构化计划时的既有重试提示 | 无 |
| [planner/tools.toml](planner/tools.toml) | Planner 的结构化计划字段说明，以及 `submit_vehicle_plan` / `load_skill` 工具说明 | 无；参数范围、计划校验和执行逻辑仍在代码中 |
| [vehicle/functions.toml](vehicle/functions.toml) | 构造 10 个 skill、100 个模拟车控函数及其参数说明；Talker 热工具、Planner 和 Thinker 的 `load_skill` 共用 | 无；`skills.<id>.name/description`、`functions.<name>.description/parameters.<参数名>` |

### Thinker

| 文件 | 使用时机 | 可用变量 |
|---|---|---|
| [thinker/research.md](thinker/research.md) | 研究、比较和多条件任务的固定 system message | `{{skill_guide}}`：代码生成的 skill ID 与名称索引 |
| [thinker/quick.md](thinker/quick.md) | 由 Talker 明确选择 `query_mode=quick` 的任务的固定 system message | 无 |
| [thinker/revision.md](thinker/revision.md) | 用户更新任务条件、从已有 checkpoint 继续时的说明；新条件在同条 JSON 消息中提供 | 无 |
| [thinker/retry.md](thinker/retry.md) | 模型只输出普通文字、未调用任务工具时的既有重试提示 | 无 |
| [thinker/finalize.md](thinker/finalize.md) | 到达既有收尾阶段时，要求使用真实已有材料交付结构化结果 | 无 |
| [thinker/source_selection.md](thinker/source_selection.md) | 有限时间收尾请求中，说明如何从服务器给出的 `citable_sources` 选择 `source_ids` | 无 |
| [thinker/tools.toml](thinker/tools.toml) | 构造研究和快速模式使用的工具说明，包括搜索、读取、澄清、建议、天气和 `finish_task` | 无；`<工具名>.description/parameters.<参数名>` |

### 车书 Embedding

| 文件 | 使用时机 | 可用变量 |
|---|---|---|
| [manual/embedding_document.md](manual/embedding_document.md) | 车书建库时，作为片段文字 embedding 请求的 Corpus 指令 | 无；片段文字由调用方独立提供 |
| [manual/embedding_query.md](manual/embedding_query.md) | 用户问题检索时，作为 Query embedding 请求指令 | 无；问题文字由调用方独立提供 |

这两份指令与索引里的模型、维度、Corpus/Query 配置绑定。**修改任意一份后都必须重建车书索引**；现有索引与新指令不一致时会明确报错，不能继续混用旧向量。使用新的索引文件完成建库，再将 `MANUAL_INDEX_PATH` 指向它。具体步骤与凭证要求见 [车书检索文档](../docs/MANUAL_RAG.md)。

### 媒体与导航

| 文件 | 使用时机 | 可用变量 |
|---|---|---|
| [cockpit/tools.toml](cockpit/tools.toml) | 构造固定的 `search_media`、`control_media`、`manage_navigation` 工具说明 | 无 |
| [cockpit/feedback.toml](cockpit/feedback.toml) | 根据真实内容查询、设备执行或路线失败生成固定反馈 | 无 |

内容提供商及端侧查询的选择不在本目录修改，见 [运行服务配置](../docs/SERVICE_PROVIDERS.md)。

### 固定话术

| 文件 | 使用时机 | 可用变量与查找方式 |
|---|---|---|
| [speech/receipts.toml](speech/receipts.toml) | 根据实际车端回执生成成功明细；`labels` 保存座位、分区和开关等名称 | 按函数名查找。具体 key 与变量见下表 |
| [speech/persona.toml](speech/persona.toml) | 三种人设设置、固定回执表达、委托/澄清/拒绝等开头和语气词 | 只有 `delegated.text` 接收 `{{start}}`、`{{goal}}`；其余 key 无变量 |
| [speech/feedback.toml](speech/feedback.toml) | 车控下发提示、后台进度、地点/天气查询、车书页图、朗读格式与执行错误 | 具体 key 与变量见下表；未列出的 key 无变量 |
| [speech/thinker.toml](speech/thinker.toml) | 后台结果的固定播报、证据限制、来源列表及工具提示 | `forecast.record` 接收 `{{location}}`、`{{date}}`、`{{minimum}}`、`{{maximum}}`；`source_item` 接收 `{{title}}`、`{{url}}`；其余 key 无变量 |
| [speech/vehicle.toml](speech/vehicle.toml) | 模拟执行器拒绝原因、模拟导航与曲目状态说明 | `rejections.seat_axis_range` 接收 `{{axis}}`、`{{lower}}`、`{{upper}}`；`state.track` 接收 `{{index}}`；其余 key 无变量 |

`speech/receipts.toml` 的占位符：

| Key | 变量 |
|---|---|
| `set_climate_power.text`、`set_window_child_lock.text`、`set_mirror_heating.text`、`set_steering_heating.text`、`set_ambient_light.text`、`set_tailgate_open.text`、`set_rear_wiper.text` | `{{enabled}}` |
| `set_temperature.text`、`adjust_temperature.uniform`、`adjust_temperature.item` | `{{zone}}`、`{{temperature}}` |
| `adjust_temperature.mixed` | `{{temperatures}}` |
| `set_fan_speed.text`、`set_rain_sensitivity.text` | `{{level}}` |
| `set_window_position.partial`、`set_sunroof_position.text`、`set_sunshade_position.text` | `{{position}}` |
| `set_window_position.text` | `{{seat}}`、`{{detail}}` |
| `set_seat_heating.level`、`set_seat_ventilation.level` | `{{seat}}`、`{{level}}` |
| `set_seat_heating.off`、`set_seat_ventilation.off`、`set_seat_massage.off` | `{{seat}}` |
| `set_seat_massage.mode` | `{{seat}}`、`{{mode}}` |
| `set_seat_position.degrees`、`set_seat_position.percent` | `{{value}}` |
| `set_seat_position.text` | `{{seat}}`、`{{axis}}`、`{{target}}` |
| `set_interior_light.text` | `{{zone}}`、`{{enabled}}` |
| `set_volume.text` | `{{volume}}` |
| `set_navigation_destination.text` | `{{destination}}` |
| `set_mirror_fold.text` | `{{folded}}` |
| `set_reading_light.text` | `{{seat}}`、`{{enabled}}` |
| `set_ambient_color.text` | `{{color}}` |
| `set_ambient_brightness.text` | `{{brightness}}` |
| `set_child_lock.text` | `{{side}}`、`{{enabled}}` |
| `set_door_lock.text` | `{{door}}`、`{{locked}}` |
| `set_tailgate_height.text` | `{{height}}` |
| `set_wiper_mode.text` | `{{mode}}` |
| `set_wiper_interval.text` | `{{seconds}}` |
| `wash_windshield.text` | `{{target}}` |

100 个车控函数均有按函数名索引的独立回执配置。没有专用短句的函数也保留自己的 `.text`，修改工具说明不会顺带改变它的播报。

以上数值、标签和状态词由代码根据真实回执格式化后传入。例如温度可能已转换为中文数字，`{{enabled}}` 是当前开关状态的文字。不要用固定字面量替掉代表实际执行结果的变量。未列出的 key 都是无变量的标签或固定片语。

`speech/feedback.toml` 的占位符：

| Key | 变量 |
|---|---|
| `dispatch.text` | `{{action}}` |
| `dispatch.set_navigation_destination` | `{{destination}}` |
| `dispatch.set_temperature` | `{{temperature}}` |
| `dispatch.set_window_position.closed`、`dispatch.set_window_position.open` | `{{seat}}` |
| `dispatch.set_window_position.partial` | `{{seat}}`、`{{position}}` |
| `task.read_sources` | `{{count}}` |
| `task.db.step` | `{{step}}` |
| `places.single`、`places.navigation_started` | `{{name}}` |
| `places.few`、`places.many` | `{{count}}`、`{{names}}` |
| `errors.range` | `{{label}}`、`{{minimum}}`、`{{maximum}}`、`{{unit}}` |
| `errors.vehicle_failure` | `{{reasons}}` |
| `maps.area_not_found` | `{{near}}` |
| `manual.page_caption` | `{{page}}` |
| `format.json_item` | `{{label}}`、`{{value}}` |

## 怎么修改

长提示词用 Markdown；工具说明和固定话术用 TOML；原生消息示例保持 JSON。只改文字、保留文件路径、key、变量名与示例消息协议，是最直接的修改方式。

例如修改空调成功回执，在 `speech/receipts.toml` 中找到：

```toml
[set_temperature]
text = "{{zone}}空调调到{{temperature}}度了。"
```

可以换句式，仍需保留 `{{zone}}` 和 `{{temperature}}`。每个模板只接受它原有的那组变量；不能从其他模板随意借用变量。变量值按一次替换插入，不递归解释其中的 `{{…}}`。

TOML 的多行文字可写成：

```toml
[finish_task]
description = """同时提交完整报告summary与简短口语spoken_summary，不增加润色步骤。
只能引用本任务真实搜索/读取过的来源。"""
```

这只是多行格式示例。换行也是发给模型的真实文字，编辑时需有意保留或改变。示例位于 `thinker/tools.toml`，不要在其他配置文件里新建同名 key。

工具名、参数名、类型、必填项、枚举、数值范围、权限、路由和执行逻辑都留在 Python。配置文件只能改变文字说明，不能通过新增一段说明创建新工具或放宽执行权限。内部参数校验异常、trace 诊断和日志也留在代码中。修改 `talker/examples.json` 时，保留合法的原生消息结构和对应的 `tool_call_id`。

## 校验与生效

从仓库根目录执行：

```sh
.venv/bin/python -m scripts.validate_prompt_config
```

启动时也会校验配置：缺 key、多 key、变量名不一致、非法模板、无效 TOML/JSON 会直接报错；不会用默认文案掩盖残缺配置。校验通过说明配置接口完整，工具选择或口语质量的变化仍需行为评测确认。

配置在启动时读取并缓存，请求路径不读取文件。**改完后重启 API 和 Thinker worker 才会完整生效**，仅刷新前端页面不够；当前没有热重载接口。worker 与 API 是独立进程，分别持有各自启动时加载的配置。

Talker 的拼接顺序为：

```text
固定 system 规则 → 人设 → 原生示例 → 历史 → 会话快照 → 本轮提示与用户输入
```

Planner 和 Thinker 同样把固定规则放前面，动态音区与任务数据放后面。工具说明与 schemas 通过 API 的 `tools` 字段提供，不是追加到对话里的普通文字。保持固定内容靠前，有助于方舟复用前缀缓存。

修改提示词、示例或工具说明后，只有使用了这些内容的请求受到影响：从内容首次变化处开始，后续部分可能需要重新预填充；更早且相同的前缀仍有机会复用。不会因此清空所有模型、会话或任务的缓存，也不意味着所有请求都必须重算。后续相同前缀可再次形成命中，实际以方舟 `usage.cached_tokens` 为准。仅修改不进入模型输入的固定播报话术，不会改变该模型请求的前缀。

# 用户座位与默认车控目标

验证日期：2026-10-16。当前原型的座位来自会话设置或输入事件的结构化元数据，不包含麦克风声源定位。

## 行为与接口

- 新会话默认主驾，`DEFAULT_SPEAKER_SEAT`可配置四个音区；前端输入框上方可切换“当前音区”。
- 每轮开始固定座位，Talker动态system快照、Vehicle Planner动态system消息、后台任务上下文使用同一来源。切换默认音区不改变正在处理的轮次。
- 未指定位置及“我的/我这边”采用当前音区；明确位置、全部范围和近期明确指代优先。后排温度共用rear，顶灯按前后排，儿童锁按同侧，玻璃清洗按前后排对应挡风玻璃；整车功能不增加位置参数。
- 从工具schema生成23个位置写操作的默认参数与调用路线。热功能仍用原生工具，其他功能经Vehicle Planner；模型决定工具参数，代码不匹配用户语义或覆盖模型输出。Thinker接收座位和默认区域，不接收执行路线表。
- 动态音区放在稳定prompt与历史之后，不增加串行模型调用。接口细节见[INTERFACES.md](INTERFACES.md)。

## 验证证据

- 最终完整模型回归：**237/237**，重复3次；音区用例**48/48**，无新增失败。报告`data/evals/speaker-zone-release-final.json`。与修改前共同的216次评测由189/216提升至216/216。
- 最终全量播报审阅：173段、126段含调用前话术，提前宣布完成0段，自然度平均4.13/5。报告`data/evals/speaker-zone-release-final.speech.json`。仅作话术参考，不替代结构化行为判分。
- 后端完整测试：1082通过，`data/speaker-zone-final-pytest.log`。
- 前端Node测试：59通过，生产构建成功，日志为`data/speaker-zone-final-frontend-tests.log`和`data/speaker-zone-final-build.log`。
- 修改前完整模型评测：189/216。新增音区用例15/42；原有用例174/174。
- 前一轮完整模型回归：231/231，包含音区用例42/42，报告`data/evals/speaker-zone-release.json`。补充儿童锁和玻璃清洗后，中间完整回归为232/237及235/237：暴露了规划层追问默认位置、历史指代被音区覆盖和一次地点名称翻译问题。保留这些报告，不将专项通过视为完整回归通过。
- 最新音区专项：48/48，重复3次，报告`data/evals/speaker-zone-reference-verified.json`。修正方式为规划层明确使用各函数的默认位置参数、动态system优先解析明确对象/历史指代、原生工具对照示例；没有增加模型调用。
- 最新专项播报审阅：32段，31段含调用前话术，宣布完成0段，自然度平均4.41/5，报告`data/evals/speaker-zone-reference-verified.speech.json`。审阅模型只收到用户原话与播报，未收到音区；其关于“未指定座位”的意见不能用于否定已明确的音区默认值。行为仍以结构化回执评测为准。
- 修正人设工具签名比较：JSON数字24与24.0按相同数值比较；布尔和字符串仍保持类型区别，不改变工具参数或行为判分。
- 浏览器实测右后排输入“座椅通风调到二档，再打开我的阅读灯。”，车端成功执行`set_seat_ventilation(seat=rear_right, level=2)`及`set_reading_light(seat=rear_right, enabled=true)`，其他座位通风与阅读灯保持初始状态。原始模型输入、调用、规划及回执保存在`data/speaker-zone-final-browser-audit.json`；截图`data/speaker-zone-final.png`。
- 最终prompt部署后，新会话默认为主驾；前端切到副驾输入“座椅加热调到一档。”，回执只将副驾加热设为1，其他座位保持0。轨迹`data/speaker-zone-deployed-audit.json`，截图`data/speaker-zone-deployed.png`。本地API和Thinker均已重启加载最终代码。
- 强化历史指代后再次部署：当前音区副驾，明确请求“右后排座椅加热开到三档。”后再说“现在把它关掉。”，两条回执均针对rear_right，分别为3档和0档，副驾未被更改。轨迹`data/speaker-zone-reference-deployed-audit.json`，截图`data/speaker-zone-reference-deployed.png`。
- 轨迹核对：Talker和Planner的system均包含右后排音区；执行回执与目标一致。该复合请求有一次Talker重试，因此进行时话术重复；原有策略允许校验重试前的话术先播出，不以文本过滤隐藏。

评测报告、运行日志、截图及SQLite不提交Git。冷车控仍包含规划耗时；音区数据不会消除模型服务波动或既有校验重试。

## 延迟

指标是文字输入后首句可送TTS的时间，不是实际首PCM播放时间。模型调用均值仅计Talker，不包含Planner内部调用。

| 范围 | 首句可播P50 / P90 | 完成P50 / P90 | Talker调用均值 |
| --- | --- | --- | --- |
| 修改前共同216次 | 814 / 1252 ms | 1816 / 4312 ms | 1.20 |
| 修改后共同216次 | 1022 / 2416 ms | 2120 / 7306 ms | 1.20 |
| 最终完整237次 | 1027 / 2618 ms | 2198 / 7316 ms | 1.23 |

本次没有新增音区判定模型调用，但实测尾部延迟上升，不能宣称延迟无影响。动态位置数据增加输入，正确路由的冷功能需要规划；另外同一工作区同时存在地点/车书改动和远端服务波动，不能将全部变化归因于音区。原有校验重试仍可能重复进行时话术，冷车控完成时间P90在本轮为9164ms。

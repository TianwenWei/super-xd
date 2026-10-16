# 当前位置与实时当地时间验证

默认配置保存在本地 `.env`：`DEFAULT_LOCATION=Paris`、`DEFAULT_LOCATION_COUNTRY=France`、`DEFAULT_TIMEZONE=Europe/Paris`。代码和固定 prompt 不写死 Paris；车辆设置可更新 `location/location_country/timezone`，新会话从环境配置取得初值。清空位置后 `current_location=null`，不使用 GPS 或服务端 IP 猜位置。

每次 Talker 请求只在历史后的动态快照中注入 `current_location/current_utc/current_local/current_date/timezone`，当地时间从同一个 UTC 时刻经 IANA 时区转换。时间持续刷新，处理跨午夜和夏令时。后台任务保留用户提供的位置与登记时车况；工具集保持固定，没有新增串行模型调用。已配置的位置属于用户提供的数据，可作为地点参数来源；未授权地点仍被既有来源校验拒绝。

## 三轮完整评测

| 指标 | 修改前 | 最终版本 |
|---|---:|---:|
| 通过率 | 211/231（91.34%） | 226/231（97.84%） |
| 首句可播 P50 | 848 ms | 883 ms |
| 首句可播 P90 | 1168 ms | 1380 ms |
| 平均模型调用次数 | 1.23 | 1.27 |

这里的首句可播是 harness 的文本管线指标，不是浏览器 PCM 渲染或扬声器声学出声。最终 P90 和平均调用次数高于基线，不能宣称所有延迟指标改善。

新增位置用例 15/15：默认位置天气（含三个人设）、附近搜索、明确指定其他城市优先。原有缺少位置的导航用例显式清空位置配置，仍检查先询问位置。地名参数允许明确列出的等价国家名称/标点写法，仍按结构化字段核对城市、日期及工具，错误城市或日期不能通过；人设工具参数一致性检查保持不变。

| 用例 | 修改前 | 最终版本 |
|---|---:|---:|
| configured_location_weather | 0/9 | 9/9 |
| configured_location_nearby | 0/3 | 3/3 |
| configured_location_explicit_city_takes_precedence | 3/3 | 3/3 |
| planner_hold_kept | 3/3 | 3/3 |

完整评测仍有 5 个失败：`speaker_cold_reading_light` 0/3、`speaker_cold_seat_support` 2/3、`speaker_history_reference_preserved` 2/3。其中历史指代用例相对基线新增一次失败：切换音区后的追问没有产生预期的左后排座椅加热关闭指令。这是实际工具参数/调用失败，保留记录，不能宣称完整评测无回归。本次未更改音区路由、车控规划和执行协议；音区问题需继续独立验收。

第一次候选方案的新增位置原生示例曾使保持项失败，已撤回这些示例，最终只保留动态快照和通用上下文规则。中间失败报告保留，最终保持项恢复 3/3。

原始本地报告（均不提交 Git）：
- `data/evals/location-context-before-20261015.json`
- `data/evals/location-context-after-20261015.json`
- `data/evals/location-context-hold-recheck-20261015.json`
- `data/evals/location-context-minimal-recheck-20261015.json`
- `data/evals/location-context-final-20261015.json`

## 真实轨迹抽查

已重载本地 8000 服务，以新会话 `9cb375e6b1464764` 验证两轮：
- 询问当前位置和当地时间：同一次模型请求中提供 Paris / France、Europe/Paris 和实时当地时间；未调用工具，回答时间与快照一致。
- 未指定城市询问天气：工具实际请求 `get_weather(location="Paris 法国", date="today")`；真实天气结果返回后由同一个 Talker 回答，第二次请求中的时钟已刷新，未再询问城市。

完整模型输入、模型输出、工具请求与结果已保存于 `data/evals/location-context-live-20261015.json`，沿用现有脱敏路径。

离线检查覆盖城市/时区可替换、未知位置、跨午夜、夏令时回拨、无时区时钟拒绝、设置原子性、导航不被位置设置触发、固定前缀不随时间改变、地点来源校验和评测选项的错误城市/日期拒绝。相关单元检查通过，前端构建通过。浏览器确认新会话默认 Paris / France / Europe/Paris，位置输入位于默认折叠的驾驶环境设置内。

## 话术抽查

最终报告运行了 `evals.speech_review`，审阅 162 条话术，自然度均分 3.88/5，进行时话术中宣布完成 0 条。该审阅器只读取用户输入和话术，不读取会话快照，因此部分“默认巴黎地点来源不明”的评价缺少已配置位置这一背景；位置来源已通过真实请求轨迹核实。话术结果仅作参考，不替代结构化评测。报告位于 `data/evals/location-context-final-20261015.speech.json`。

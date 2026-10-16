# 文案配置迁移验证记录

验证日期：2026-10-16。迁移前版本：`4adcf4a`。配置入口和修改方法见 [prompts/README.md](../prompts/README.md)。

## 范围

- 18 个主模板从 `backend/prompts/*.txt` 原样移到根目录 `prompts/` 下的模块目录，原生示例仍为 JSON，共 78 条消息。
- 9 个 TOML 保存 675 项说明与话术。模型说明含 Talker 22 项、Planner 6 项、Thinker 15 项、车控目录 237 项；固定话术及标签共 395 项。
- 100 个车控函数都有独立回执配置。工具说明和播报可分别编辑。
- 参数类型、范围、枚举、工具边界、车辆执行、事务和 revision 逻辑仍在代码中。内部异常、结构化校验诊断、trace 标记和数字格式转换仍在代码中。
- API 和 Worker 启动均检查完整文件清单、key、占位变量和原生示例的消息协议、工具名及参数 Schema。请求时只使用启动缓存，没有新增模型调用或文件读取。
- 本次按维护者明确授权同步更新 AGENTS.md 的目录与配置规范，未改变其他权限规则。

## 确定性验证

完整单元测试：**1,219 passed**。

迁移前后主模板、原生示例、人设请求以及 Talker 全部工具 JSON 逐字节相同；30 组完整 Talker/Thinker 模型请求在相同环境、相同动态数据下完全相同。车控的完整 ToolSpec、OpenAI Schema 和 SkillManifest JSON 也逐字节相同，另核对 12 组 Planner Schema 和 4 组音区上下文。

固定输出对照 **1,931/1,931 相同**：1,770 组回执、人设、任务和地点输出，146 组地图、天气、车书、朗读与模拟器输出，15 组任务状态及步骤摘要。另对照了 126 个车控执行场景的回执与车辆状态。

验证材料保存在本机忽略目录：`data/prompt-config-input-*.json`、`data/prompt-config-capture-*.json`、`data/prompt-config-equivalence/`、`data/prompt-config-catalog-equality.json` 和 `data/prompt-config-planner-equality.json`。

## 真实模型评测

前后均运行完整 `evals.harness --repeat 3`，默认并发为 4；没有修改提示词内容、示例、用例或评分标准。

| 指标 | 迁移前 | 迁移后 |
|---|---:|---:|
| 通过 | 237/237（100%） | 235/237（99.16%） |
| 首句可播 P50 | 12,329 ms | 10,631 ms |
| 首句可播 P90 | 18,102 ms | 17,657 ms |
| 平均模型调用次数 | 1.22 | 1.23 |

报告分别为 `data/evals/prompt-config-before.json` 和 `data/evals/prompt-config-after.json`。两轮评测有部分时间重叠，包含供应商响应、并发负载和采样差异；不能据此认定文件整理提升了模型推理速度。代码路径没有新增模型调用，模型请求内容也保持一致。

迁移后有两条用例各失败一次，完整报告保留这些失败：

- `configured_location_nearby`：模型把已知位置 `Paris` 翻译成 `巴黎` 放进 `search_places.area`，被既有地点来源校验拦截，未搜索；重试后又询问位置。
- `hold_ambient_not_closed`：模型将“先别开”生成了关闭氛围灯的调用，多执行了一个本应保持不动的动作。

同一份迁移后代码对这两条用例各复测三次，**6/6 通过**，报告为 `data/evals/prompt-config-failed-recheck.json`。额外对照了这两个原用例的输入、初始车况和人设：两版本在相同环境与冻结时间下，每个完整首轮请求的 83 条消息和 23 个工具均逐字节相同，JSON 插入顺序也一致；证据为 `data/prompt-config-failures-capture-*.json` 和 `data/prompt-config-failure-analysis.json`。这些结果与采样波动的解释一致，但两轮评测不足以单独证明失败原因。本次没有为了这些失败改写提示词、补用例专属规则或加入文本匹配；这两个行为场景仍需后续持续评测，复测结果不覆盖原始全量结果。

`evals.speech_review` 对迁移后报告中的 178 段不同回复进行了模型抽查：自然度平均 **4.10/5**，125 段有前置话术的回复中，回执前宣称完成 **0 次**。这是模型辅助判断，作为参考记录；报告为 `data/evals/prompt-config-after.speech.json`。

## 部署与真实链路

API 和 Worker 已重启并成功加载新配置，健康检查、车书索引和前端代理正常。

通过前端代理注入 PCM 验证连续 VAD → 火山 ASR → Talker → 实际模拟车端回执 → 火山 TTS，并验证第二次说话打断音频。收到非零 TTS PCM，持续监听连接未断开。这验证了服务链路；未测试物理麦克风或浏览器回声消除。

抽查会话 `b59870930b004503`：识别内容要求设置空调 23 度；模型先说“这就把主驾空调温度设为23度”，调用 `set_temperature(zone=driver, temperature=23)`，真实模拟器回执确认主驾 23 度、其他温区保持 22 度。请求仍为 23 个工具，动态快照在固定规则、人设和原生示例之后；本轮只有一次模型调用。原始 trace 为 `data/main-redeploy-vad-deployed.trace.json`。

后续修改配置先运行 `.venv/bin/python -m scripts.validate_prompt_config`，再重启 API 和 Worker；embedding 指令修改还需重建车书索引。

# 轨迹核验方法

生产诊断与普通显示流分离，协议见[Gateway契约](GATEWAY_CONTRACT.md#诊断)。Demo通过[轨迹接口](INTERFACES.md#轨迹读取)和前端抽屉查看；截图或一条成功日志不能替代完整链路核验。

每个样本按run/turn/task/revision及call/invocation ID关联：实际模型输入、固定工具、原始返回、参数、校验结果、交接、真实回执、最终表达与audio.text。保留provider实际返回的model/usage/cache/finish_reason及错误码，未返回的字段保持缺失；reasoning_content只显示实际收到的字段，不补造内部思考。

重点检查：

- 新输入、取消和旧revision结果仍归属原身份；stale记录不能产生新工具或覆盖当前回答。
- 计划、预检、发出请求、ACK和真实执行成功分别可辨；unknown不产生自动重发。
- 查询的原文/页码/URL、范围注解和进展指纹可回溯；引用存在不证明答案完整正确。
- checkpoint使用紧凑恢复状态，完整模型请求与工具schema仍可在诊断找回；模型副本压缩不修改原回执。
- text.delta批量写入，挂起/交接/终态和取消关闭前刷新；诊断提交失败不能释放未审计执行请求。
- 凭证字段、已知secret值和媒体能力URL脱敏；资料来源URL保留，PCM二进制不塞入JSON。
- Demo分页游标跨页无遗漏/重复；筛选不串会话，长内容能完整展开/复制，增量记录不强制改变阅读位置。

```sh
.venv/bin/python -m pytest -q tests/test_traces.py tests/test_gateway_diagnostics.py tests/test_gateway_recovery.py
```

生成的完整trace和截图放在忽略的 `data/validation/`，不提交Git。报告记录版本、样本身份、检查项和失败理由，不宣称旧历史是当前已验收结果。

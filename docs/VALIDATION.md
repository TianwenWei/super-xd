# 当前验收方法

本文记录方法和口径，不把旧版本的测试数量、截图或成功样本当作当前验收。每批报告必须注明 git/release/model/semantic policy 版本、配置来源、输入集、重复次数、执行环境与实际模型。生成的截图、音频、完整 trace 和报告放在忽略的 `data/validation/` 或 `data/evals/`，不提交 Git；对比保留原始失败与取消样本，不覆盖旧输出。

## 离线门槛

```sh
.venv/bin/python -m scripts.validate_prompt_config
.venv/bin/python -m scripts.validate_service_config
.venv/bin/python -m scripts.check_import_boundaries --smoke-startup
.venv/bin/python -m pytest -q
cd frontend
node --test tests/*.test.mjs
npm run build
```

重点核对固定26工具、完整参数与禁止动作、车辆保持项、来源/版本、任务revision/租约、未知回执不重发、取消与迟到结果隔离、checkpoint v2恢复/体积边界、纯NLP安装和启动无Demo依赖。测试替身只证明契约及状态转换，不证明真实模型、外部Gateway或真车效果。

## 真实行为评测

使用服务配置中的模型角色和凭证选择器，至少重复3次；生产与评测都经NLPService。先冻结泛化输入和期望，再改Prompt，不将评测原句复制到few-shot。

```sh
.venv/bin/python -m evals.harness --repeat 3 --out data/evals/acceptance-run.json
```

报告完整通过率、新增失败、禁止动作率、精确参数、每persona及跨persona差异；首句P50/P90和平均模型调用数按相同样本口径比较。带语义rubric的轮次自动由独立评委给出逐项结论和材料原文引用，unreviewed不能计为通过；脱敏评委原始响应和解析失败原因保留，仍抽查完整模型输入、工具、回执与实际回复。评委调用单独统计，不计入生产轮次。来源存在不等于回答受支持，工具成功不等于用户授权或答案正确。

跨领域场景包括咨询/执行、证据覆盖与时间粒度、完整生成与部分播放追问、无新证据收束、地点/音区/任务版本切换。不要用固定话术字串判断自然语言语义，也不要以ASR错别字代替工具和回答质量判断。

## 集成与表现

真实Gateway核对原子交接、幂等、租约/CAS、断线/重复/迟到回执、unknown核对、取消、能力变化与升级。设备实际效果和安全保护由执行团队验证，NLP结构校验不能替代物理验证。

音频按[音频验收](AUDIO_VALIDATION.md)，界面按[UI验收](../frontend/UI_VALIDATION.md)，诊断按[轨迹核验](TRACE_VALIDATION.md)。首字、供应商首包、程序收到PCM、浏览器渲染和扬声器出声分别报告；仅文本快或离线测试通过不等于语音/真车通过。

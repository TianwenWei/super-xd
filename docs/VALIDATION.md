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

`allow_error`只允许场景预期的业务或工具失败，任何实际`model_errors`仍判失败，不能掩盖模型超时。已有等价工具入口可用结构化候选表示，但必须继续核对精确执行次数、参数和最终状态。

跨领域场景包括咨询/执行、证据覆盖与时间粒度、完整生成与部分播放追问、无新证据收束、地点/音区/任务版本切换。不要用固定话术字串判断自然语言语义，也不要以ASR错别字代替工具和回答质量判断。

## 集成与表现

真实Gateway核对原子交接、幂等、租约/CAS、断线/重复/迟到回执、unknown核对、取消、能力变化与升级。设备实际效果和安全保护由执行团队验证，NLP结构校验不能替代物理验证。

音频按[音频验收](AUDIO_VALIDATION.md)，界面按[UI验收](../frontend/UI_VALIDATION.md)，诊断按[轨迹核验](TRACE_VALIDATION.md)。首字、供应商首包、程序收到PCM、浏览器渲染和扬声器出声分别报告；仅文本快或离线测试通过不等于语音/真车通过。

Talker响应速度的主指标是输入提交到TTS实际首帧PCM，另列首个非零PCM；浏览器实收按音频验收口径统计。服务端 `server_first_audio_ms` / `server_first_nonzero_audio_ms` 从该轮输入开始计时；`first_speech_ms` 仅表示文本送入TTS，不能代替音频首帧。ASR/VAD耗时和TTS收到文本至首帧的自身耗时分别记录。预告音频可计入首帧响应，但查询/动作是否最终完成仍独立验收；首帧较快不能豁免后续超时。

交互延迟还须覆盖新会话、用户停顿后续聊、重复车控后的历史、零缓存和缓存命中、首次网络超时及已出字后断流。按供应商实际 `cached_tokens=0`、`>0` 和未提供分别统计，不能把连续密集调用当作冷缓存验收，也不能假定固定前缀必定命中。记录每次网络尝试和逻辑累计耗时；超时/取消数与无首字样本必须保留，成功样本的P50/P90不能代替整体可用率。首字预算测试须证明空包或思考片段无法续命、重试不重置期限、取消会关闭上游且不污染后续轮次。

闲聊延迟使用完整生产编译链、固定26工具和正常上下文，不能用裸模型的短请求冒充产品结果。以下基准串行覆盖问候、闲聊和感谢；每条输入新建会话，共享冻结模型及连接池，意外工具请求直接失败，不实际执行。分别跑连续组和停顿组，期间不要同时运行其他真实模型评测：

```sh
.venv/bin/python -m evals.latency --repeat 3 --max-first-text-ms 2000 --out data/evals/chat-consecutive.json
.venv/bin/python -m evals.latency --repeat 3 --pause-seconds 30 --max-first-text-ms 2000 --out data/evals/chat-paused.json
```

这里的2000毫秒是可调整的首模型文字检查线，不是服务已达到的承诺。失败、无模型文字、超过检查线或误调用工具均退出非零；报告保留慢成功样本、原始trace、实际停顿时间和缓存分组。首次调用不等于零缓存，错误模板不等于模型首字。此基准不覆盖ASR、浏览器显示和TTS首声；重启后的本地WebSocket及音频链路另行测量。

工具往返与多轮上下文沿用行为评测，可用 `--case` 或 `--tag` 选择已冻结的用例，并增加独立的首个有用结果检查线；这项用于检查完整回答/执行结果，不是Talker的TTS首帧指标：

```sh
.venv/bin/python -m evals.harness --concurrency 1 --repeat 3 --pause-seconds 30 --max-first-useful-ms 5000 --out data/evals/tool-roundtrip-paused.json
```

5000毫秒同样只是可调整的验收线。门槛只使用 `first_useful_ms`：最终回答所在模型调用的首文字，或已确认执行的回执/统一完成播报；工具预告和失败模板不能代替。错误、结果未知、指标缺失和超过检查线均失败，即使场景允许业务错误也不能豁免延迟门槛。原行为失败与语义评审仍保留，延迟成功不能覆盖它们。

正数 `--pause-seconds` 要求 `--concurrency 1`，保证上一轮结束至下一轮开始至少间隔指定秒数，包括同一用例内、用例间及重复批次间；不会人为延迟一轮内部的工具续推理。每轮 `actual_idle_ms` 记录实际间隔，第一轮为空。报告分开首次、连续、停顿和未知节奏，以及零缓存、命中和未知缓存；保留无有用结果/门槛失败数量、全部轮次累计模型调用、原始 trace 与慢样本。并行默认模式不宣称测得串行空闲时间。

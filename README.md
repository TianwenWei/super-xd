# Super XD — NLP Agent Harness

车载语音助手的NLP推理与工具编排服务，包含Talker、前台车控Reasoner和后台研究Reasoner。外部Gateway负责鉴权、会话/上下文、任务账本、设备连接和工具交接；设备团队执行动作并返回真实结果。

生产核心位于[`backend/harness/`](backend/harness/)，入口为[`backend/nlp_app.py`](backend/nlp_app.py)。原座舱Demo作为本地验证适配器保留，使用共享推理核心；生产NLP服务不启动SQLite、ASR/TTS、WebSocket或模拟车辆。

- [架构、运行与验证边界](docs/NLP_HARNESS.md)
- [Gateway V1输入、交接、续跑、取消与任务账本契约](docs/GATEWAY_CONTRACT.md)
- [开发约束](AGENTS.md)

## 启动NLP服务

需要Python 3.12。创建虚拟环境并安装`requirements.txt`，把`.env.example`复制为`.env`，填写模型凭证、`NLP_GATEWAY_URL`及所选检索服务配置。

```sh
.venv/bin/python -m scripts.validate_prompt_config
.venv/bin/python -m scripts.validate_service_config
.venv/bin/python -m uvicorn backend.nlp_app:configured_app --factory --host 127.0.0.1 --port 8001
```

`/v1/runs/start`与`/v1/runs/resume`返回NDJSON流；`/v1/runs/cancel`取消本实例活动请求。`/v1/tasks/execute`推进Gateway已授权并持有租约的后台任务，`/v1/tasks/mutate`提交reply/update/cancel，`/v1/tasks/present`按真实任务版本生成回流播报和卡片。接口文档在`/docs`，进程版本在`/health/live`。

Gateway契约为本项目拟定并实现的目标接口，尚待外部联调。NLP入口只对可信内部Gateway开放；checkpoint与原始诊断不能直接暴露给车端。缺配置或凭证明确报错，无伪造执行或静默替代服务。

模型与资源提供商在[`config/services.toml`](config/services.toml)选择。NLP生产装配只创建模型、任务检索/天气、Gateway任务和诊断客户端；资源及设备动作通过结构化交接执行。服务适配器说明见[服务配置](docs/SERVICE_PROVIDERS.md)。

## Prompt维护

行为主规则只维护三份：

| 文件 | 内容 |
| --- | --- |
| [`prompts/talker.md`](prompts/talker.md) | 决策、工具、表达、三种人设及上下文section |
| [`prompts/reasoner_foreground.md`](prompts/reasoner_foreground.md) | 前台车控计划、权限边界、意图账本和校验反馈 |
| [`prompts/reasoner_background.md`](prompts/reasoner_background.md) | quick/research、澄清、修订、检索与收尾 |

工具说明、原生few-shot、固定播报、embedding指令保留结构化配置。启动时校验并缓存，请求只选择当前模式/阶段需要的section。车况、音区、时间、任务进度和查询结果仍是运行时数据。完整索引见[prompts/README.md](prompts/README.md)。

## 本地座舱验证台

```sh
./scripts/start.sh
```

脚本启动Demo API（8000）、SQLite后台调度和Vite（5173）。需要Node.js 22+；语音、地图、车书功能分别按`.env.example`配置凭证。也可分终端运行：

```sh
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
.venv/bin/python -m backend.worker
cd frontend && npm run dev -- --host 127.0.0.1
```

Demo车辆为模拟；媒体的浏览器播放结果来自真实播放器事件。设备执行团队接入不依赖Demo前端。车书检索保留原文、页码与整页图片，不根据某一本手册推断其他车型配置，也不从手册资料取得执行权限。

## 测试与行为评测

```sh
.venv/bin/python -m pytest -q
(cd frontend && node --test tests/*.test.mjs && npm run build)
.venv/bin/python -m scripts.validate_prompt_config
.venv/bin/python -m scripts.validate_service_config
```

真实模型评测需要供应商访问权限，会产生调用费用；车辆执行仍由测试模拟：

```sh
.venv/bin/python -m evals.harness --repeat 3 --out data/evals/run.json
.venv/bin/python -m evals.speech_review data/evals/run.json
```

评测与服务使用同一份`SERVICES_CONFIG_PATH`、角色模型和profile的`key_env`，报告记录实际配置来源及模型。评测比较结构化调用、参数、结果与人设一致性，报告首句P50/P90和模型调用次数；话术审查只作辅助。本轮结果与评测曾误用凭证的修复说明见[NLP架构文档](docs/NLP_HARNESS.md)。离线合同测试不证明真实模型质量、线上延迟或真车行为；[历史验证记录](docs/VALIDATION.md)不能当作新版本已经通过的结果。

## 其他文档

- [Demo HTTP / WebSocket](docs/INTERFACES.md)、[车控函数与skill](docs/TOOLS.md)
- [媒体与导航](docs/COCKPIT_SERVICES.md)、[服务适配器](docs/SERVICE_PROVIDERS.md)
- [车书与来源](docs/MANUAL_RAG.md)、[车书服务](docs/MANUAL_PROVIDERS.md)、[Embedding](docs/EMBEDDING_PROVIDERS.md)
- [ASR](docs/ASR.md)、[TTS](docs/TTS.md)、[语音适配器](docs/AUDIO_PROVIDERS.md)
- [轨迹验证](docs/TRACE_VALIDATION.md)、[音频验收](docs/AUDIO_VALIDATION.md)

任务、对话和诊断包含用户数据。生产持久化由Gateway负责；Demo存储在`data/`，不提交版本库。密钥、令牌和媒体能力URL在诊断路径脱敏，分享原始记录前仍需核对数据范围。

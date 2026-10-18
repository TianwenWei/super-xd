# Super XD — NLP Agent Harness

车载语音助手的NLP推理与工具编排服务，包含Talker、车控规划Reasoner和支持前台等待/后台交付的只读研究Reasoner。外部Gateway负责鉴权、会话/上下文、任务账本、设备连接和工具交接；设备团队执行动作并返回真实结果。

生产核心位于[`src/super_xd/nlp/`](src/super_xd/nlp/)，入口为[`src/super_xd/nlp/api/app.py`](src/super_xd/nlp/api/app.py)。原座舱Demo作为本地验证适配器保留，使用共享推理核心；生产NLP服务不启动SQLite、ASR/TTS、WebSocket或模拟车辆。

- [架构、运行与验证边界](docs/NLP_HARNESS.md)
- [Gateway V1输入、交接、续跑、取消与任务账本契约](docs/GATEWAY_CONTRACT.md)
- [开发约束](AGENTS.md)

## 代码组织

```text
src/super_xd/
  nlp/
    api/             # HTTP/NDJSON
    contracts/       # wire与内部数据契约
    engine/          # 模型/工具循环、预算、恢复
    orchestration/   # Talker、车控规划、同任务前后台交付
    policy/          # 纯业务规则与回执校验
    catalog/         # 工具/技能结构定义
    prompts/         # 文案加载、上下文和工具编译
    adapters/        # 模型、检索、Gateway传输
    assembly.py      # 冻结配置和资源装配
  demo/              # 本地会话、音频、SQLite、模拟设备
prompts/  config/     # 编辑资源，构建时随wheel打包
frontend/            # 本地验证界面
scripts/  evals/      # 配置/部署检查、供应商探针、行为评测
tests/{nlp,demo,evals,support}/
```

`contracts`和`catalog`不加载prompt；`engine`不依赖产品策略或具体适配器；`policy`不做网络IO；生产`nlp`不导入`demo`。静态与运行时导入检查在CI中执行。共享测试替身归`tests/support`。

## 启动NLP服务

需要Python 3.12及uv。`pyproject.toml`是依赖声明，`uv.lock`锁定实际版本。生产仅安装核心依赖，凭证由部署环境注入：

```sh
uv sync --locked --no-dev
```

本地验证安装Demo和开发依赖，复制`.env.example`为`.env`后填写凭证：

```sh
uv sync --locked --extra demo
```

`.env`只由Demo与命令行工具显式加载；生产入口不会隐式读取工作目录的凭证文件。本地运行生产入口时，可使用uv的显式环境加载参数：

```sh
.venv/bin/python -m scripts.validate_prompt_config
.venv/bin/python -m scripts.validate_service_config
uv run --env-file .env --no-sync uvicorn super_xd.nlp.api.app:configured_app --factory --host 127.0.0.1 --port 8001
```

`/v1/runs/start`与`/v1/runs/resume`返回NDJSON流；`/v1/runs/cancel`取消本实例活动请求。`/v1/tasks/execute`推进Gateway已授权并持有租约的后台任务，`/v1/tasks/mutate`提交reply/update/cancel，`/v1/tasks/present`按真实任务版本生成回流播报和卡片。接口文档在`/docs`，进程版本在`/health/live`，部署配置与可选Gateway探活在`/health/ready`，运行计数、耗时和模型用量在`/metrics`。未配置探活不会冒充Gateway连通。

Gateway契约为本项目拟定并实现的目标接口，尚待外部联调。NLP入口只对可信内部Gateway开放；checkpoint与原始诊断不能直接暴露给车端。缺配置或凭证明确报错，无伪造执行或静默替代服务。

模型与资源提供商在[`config/services.toml`](config/services.toml)选择。NLP生产装配只创建模型、任务检索/天气、Gateway任务和诊断客户端；资源及设备动作通过结构化交接执行。服务适配器说明见[服务配置](docs/SERVICE_PROVIDERS.md)。

安装采用`src`包布局；源码启动、测试和脚本统一使用`uv sync`创建的环境，不通过`PYTHONPATH`补路径。构建wheel可运行`uv build --wheel`。

wheel包含根目录的prompt和两份基础配置。默认从安装资源读取；部署如需独立资源目录，可在启动前设置`SUPER_XD_RESOURCE_ROOT`，该目录须包含`prompts/`和`config/`。`SERVICES_CONFIG_PATH`可选取外部服务配置，地区预设来自资源目录。Demo的可写数据库、索引和媒体使用`SUPER_XD_WORKSPACE`；源码模式默认为仓库根目录，安装模式默认为当前工作目录。

## 模型与任务边界

模型只接受类型化`ModelRequest`，返回`ModelResult`或`ModelEvent`流；`ModelService`按冻结的角色配置绑定供应商、模型与预算。`ChatCompletionsTransport`统一HTTP/SSE、累计期限、重试及关闭；Ark、Vertex和兼容端点分别编码请求与认证。没有旧`thinker`布尔参数、模型提示覆盖或消息`_meta`兼容接口。

`delegate_task.mode=foreground/background`选择交付方式，`query_mode=quick/research`选择研究策略，两者不扩大工具权限。任务均先登记到Gateway，再由持有租约的执行者推进；前台默认等待8秒，随后将同一任务切为后台交付。切换不更换执行者、不重置revision/checkpoint/证据/预算；当前不实现跨worker租约移交。车控仍由专门规划器提交完整计划并等待真实回执。

## Prompt维护

采用专家的八模板组织，按实际能力适配工具与交付协议：

| 文件 | 内容 |
| --- | --- |
| [`prompts/common.md`](prompts/common.md)、[`talker.md`](prompts/talker.md) | 用户处境、数据边界、即时回应与委托 |
| [`reasoner.md`](prompts/reasoner.md)、[`reasoner_foreground.md`](prompts/reasoner_foreground.md)、[`reasoner_background.md`](prompts/reasoner_background.md) | 共享研究过程与前后台交付方式 |
| [`persona.md`](prompts/persona.md)、[`context.md`](prompts/context.md) | 单一人设和本轮事实，分别注入 |
| [`talker_event.md`](prompts/talker_event.md) | 事件表达契约；当前使用确定性播报，未增加模型轮次 |

车控计划协议独立在`vehicle_planner.md`，短的包装、重试与收尾消息在`runtime.md`。工具用法保留TOML，原生示例缩减为少量协议示范；固定播报和embedding指令仍独立。启动时严格校验资源、变量、section与示例工具协议并缓存，请求不读文件。完整映射、与专家原稿的必要差异及文本测量见[prompts/README.md](prompts/README.md)。

## 本地座舱验证台

```sh
./scripts/start.sh
```

脚本启动Demo API（8000）、SQLite后台调度和Vite（5173）。需要Node.js 22+；语音、地图、车书功能分别按`.env.example`配置凭证。也可分终端运行：

```sh
.venv/bin/python -m uvicorn super_xd.demo.api.app:app --host 127.0.0.1 --port 8000
.venv/bin/python -m super_xd.demo.tasks.worker
cd frontend && npm run dev -- --host 127.0.0.1
```

Demo通过`SessionRegistry`管理会话：可显式DELETE关闭，断开且无生成/活跃任务的会话闲置30分钟后回收；SQLite记录保留，进程内会话不会自动恢复。通知按任务版本、有效期、说话/通话/驾驶状态择机交付，普通进度仅更新屏幕。

Demo车辆为模拟；媒体的浏览器播放结果来自真实播放器事件。设备执行团队接入不依赖Demo前端。车书检索保留原文、页码与整页图片，不根据某一本手册推断其他车型配置，也不从手册资料取得执行权限。

## 测试与行为评测

```sh
.venv/bin/python -m pytest -q
(cd frontend && node --test tests/*.test.mjs && npm run build)
.venv/bin/python -m scripts.validate_prompt_config
.venv/bin/python -m scripts.validate_service_config
```

评测直接运行生产`NLPService`的start/resume，与Demo共用本地Gateway适配器。设备夹具维护独立状态并返回实际回执，禁止回显NLP预期状态。真实模型评测需要供应商访问权限，会产生调用费用；车辆执行仍由测试模拟：

```sh
.venv/bin/python -m evals.harness --repeat 3 --out data/evals/run.json
.venv/bin/python -m evals.harness --tag general_principles --repeat 3 --out data/evals/principles.json
.venv/bin/python -m evals.speech_review data/evals/run.json
```

评测与服务使用同一份`SERVICES_CONFIG_PATH`、角色模型和profile的`key_env`，报告记录实际配置来源及模型。评测比较结构化调用、参数、结果与人设一致性，报告首句P50/P90和模型调用次数；话术审查只作辅助。通用行为集还用独立语义审查核对意图、证据范围和表达；审查失败或缺失不会记为通过。审查模型调用单独统计，不计入生产轮次。离线合同测试不证明真实模型质量、线上延迟或真车行为；[验收方法](docs/VALIDATION.md)列出各层验证边界。

CI包含离线Python/前端检查、核心独立安装与导入边界、渐进类型检查及容器构建。`Dockerfile`安装wheel和NLP核心依赖，默认端口8001；Docker环境可用时执行`docker build -t super-xd-nlp .`。镜像构建后还会在无网络容器内执行导入与离线启动检查。生产任务与诊断传输仍需外部Gateway部署配套。

## 其他文档

- [Demo HTTP / WebSocket](docs/INTERFACES.md)、[车控函数与skill](docs/TOOLS.md)
- [媒体与导航](docs/COCKPIT_SERVICES.md)、[服务适配器](docs/SERVICE_PROVIDERS.md)
- [车书与来源](docs/MANUAL_RAG.md)、[车书服务](docs/MANUAL_PROVIDERS.md)、[Embedding](docs/EMBEDDING_PROVIDERS.md)
- [ASR](docs/ASR.md)、[TTS](docs/TTS.md)、[语音适配器](docs/AUDIO_PROVIDERS.md)
- [轨迹验证](docs/TRACE_VALIDATION.md)、[音频验收](docs/AUDIO_VALIDATION.md)

任务、对话和诊断包含用户数据。生产持久化由Gateway负责；Demo存储在`data/`，不提交版本库。密钥、令牌和媒体能力URL在诊断路径脱敏，分享原始记录前仍需核对数据范围。

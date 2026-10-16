# Super XD — Talker / Thinker 车载语音原型

车载语音助手的双层架构原型，车辆全部为模拟：

- **Talker**（`backend/session.py`）：每个会话一个 actor，负责听、说、打断、热车控、即时查询和委托。
- **Vehicle Planner**（`backend/vehicle_planner.py`）：复合车控和非热车控的结构化计划（intent 账本），按真实回执逐项执行。
- **Thinker**（`backend/worker.py`）：独立进程，通过 SQLite 任务表做后台调研，结果以事件回流。
- **前端**（`frontend/src/`）：调试台，同时充当模拟车机。

架构约束、不变量和改动规范见 [AGENTS.md](AGENTS.md)。

## 准备

需要 Python 3.12、Node.js 22+，推荐安装 [uv](https://github.com/astral-sh/uv)。

### API 密钥

在仓库根目录创建 `.env`（从模板复制），每行一个 `变量名=值`，值不加引号：

```bash
cp .env.example .env
```

| 变量 | 是否必需 | 用途 |
|---|---|---|
| `ARK_API_KEY` | **必需** | 火山方舟大模型（Talker / Planner / Thinker）。控制台 → API Key 管理 |
| `TAVILY_API_KEY` | 网页搜索和后台调研需要 | Talker 的直接网页搜索、Thinker 的网页搜索与正文读取（tavily.com） |
| `GOOGLE_MAPS_API_KEY` | 地点/天气查询需要 | Google Places API (New) 与 Weather API，GCP 项目需开通这两个 API 和结算 |
| `VOLCENGINE_SPEECH_API_KEY` | 朗读需要 | 火山语音合成（小何 2.0，资源 `seed-tts-2.0`）。旧版控制台改填 `VOLCENGINE_SPEECH_APP_ID` + `VOLCENGINE_SPEECH_ACCESS_TOKEN` |
| `VOLCENGINE_ASR_API_KEY` | 语音输入需要 | 火山流式语音识别，与 TTS 凭证相互独立。旧版控制台改填 `VOLCENGINE_ASR_APP_ID` + `VOLCENGINE_ASR_ACCESS_TOKEN` |

示例：

```
ARK_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TAVILY_API_KEY=tvly-xxxxxxxxxxxxxxxx
GOOGLE_MAPS_API_KEY=AIzaxxxxxxxxxxxxxxxx
VOLCENGINE_SPEECH_API_KEY=xxxxxxxxxxxxxxxx
VOLCENGINE_ASR_API_KEY=xxxxxxxxxxxxxxxx
```

模型名、超时、并发等其余配置见 `.env.example`，一般保持默认。缺少某项凭证时，对应功能会直接报错，不会生成假回复或换用其他服务。密钥只在服务端读取，`.env` 和 `data/` 不提交。

细节：[TTS 配置](docs/TTS.md)、[ASR 配置](docs/ASR.md)。

Talker 固定提供 23 个工具，其中 `web_search(query, max_results?, include_domains?)` 使用 Tavily 直接搜索网页，在当前轮回答简单实时事实或明确的搜索请求。结果卡显示真实搜索摘要、来源链接与查询时间；摘要不代表已读取或核实页面全文。复杂比较、多步调研及指定来源深入核验仍走 Thinker；天气和地点仍分别使用 `get_weather`、`search_places`。

`search_manual(query, max_results?)` 支持车书问答：使用已下载的腾势 D9 中文右舵版说明书，298 页切成 385 个文字片段。火山方舟仅生成文字向量，SQLite 保存原文与向量，NumPy 在本地排序；同一个 Talker 根据检索资料回答。结果卡展示原文、页码、对应 PDF 整页图片及原始 PDF 链接。此工具不执行车控，当前车况仍来自模拟器；手册版本和文字提取限制随来源展示。配置与重建见 [车书问答](docs/MANUAL_RAG.md)。

## 运行

```bash
./scripts/start.sh
```

脚本会安装依赖，并同时启动 API（:8000）、Thinker 和 Vite（:5173）。打开 http://127.0.0.1:5173 使用车机验证台，API 文档在 http://127.0.0.1:8000/docs。日志在 `data/`，Ctrl+C 停止全部进程。服务重启后需要在界面点“新建会话”。

也可以分三个终端运行：

```bash
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
.venv/bin/python -m backend.worker
cd frontend && npm run dev -- --host 127.0.0.1
```

## 测试与评测

```bash
.venv/bin/python -m pytest -q                              # 离线单测（tests/、evals/），不调用任何外部服务
(cd frontend && node --test tests/*.test.mjs && npm run build)
```

行为评测用真实模型驱动 Talker 和 Planner，车辆仍是模拟的。用例放在 `evals/cases/*.json`，只比对结构化结果：下发的车控、Talker 工具调用、最终车况和登记的任务，不读回复文本。

```bash
.venv/bin/python -m evals.harness                          # 全部用例，需要 ARK_API_KEY；会产生模型费用
.venv/bin/python -m evals.harness --tag hold --out data/evals/hold.json
.venv/bin/python -m evals.harness --repeat 3 --out data/evals/run.json   # 模型有采样波动，比较改动前后至少重复 3 次
```

2026-10-15 的结果（42 组用例×人设，各跑 3 次）：重构前 78/126，当前 124/126。差距主要来自旧代码把人设放进了决定工具调用的那次请求（违反不变量 6）。残余失败是“先别开氛围灯”偶尔多发一条关闭指令（车况不变）。

延迟（从文字进入系统算起，不含 ASR，126 轮）：首句可播 P50 2.1 秒、P90 3.2 秒，平均每轮 1.04 次模型调用。闲聊、车控、查询都只需一次调用，文字边生成边送 TTS。

新增 `web_search` 的同组评测各重复三次，共144次：接入前128/144，最终142/144；新增用例18/18通过，包含三个人设、直接搜索、混合热车控和不搜索车况。剩余两次失败均为原有 `hold_ambient_not_closed`（多发关闭氛围灯指令；基线也有一次失败），没有新增失败用例类型。中间版本的一次闲聊误调风量，通过补充独立的纯情绪回应示例后未再出现。

这两批首句可送TTS的P50/P90为1414/3653ms与798/1690ms，平均模型调用次数为1.05与1.16。这里的首句时间不是首PCM；缓存和供应商延迟有波动，不据此保证线上加速。直接网页搜索需取得真实工具结果后再由Talker回答，通常两次模型调用；相比原来的后台接手，多了当前轮交付答案的调用。真实浏览器搜索一轮取得三个来源、没有后台任务，首非零PCM为5890ms，未达1.5秒目标。报告及完整输入/输出/工具回执审计保存在本机 `data/evals/web-search-{before,final}.json`、`data/web-search-trace-audit.json`。

话术质量另有离线审查（只作参考，不算通过标准）：

```bash
.venv/bin/python -m evals.speech_review data/evals/run.json
```

车控话术改为模型用进行时说一句（“这就把主驾调到二十三度”），成功不再重复、失败按回执补报之后，审查的口语自然度从 3.88 升到 4.57（满分 5），43 句进行时话术里没有一句提前宣布完成。

带 `personas` 字段的用例会在每个人设下各跑一遍，并检查工具调用序列完全一致（不变量 6）。带 `maps` 标签的用例还需要 `GOOGLE_MAPS_API_KEY`。修 bug 时先在这里加失败用例，再按 AGENTS.md §6 修改。

## 文档

- [接口（WebSocket / HTTP）](docs/INTERFACES.md)
- [100 个车控函数与 10 个 skill](docs/TOOLS.md)
- [Thinker 设计](docs/THINKER.md)
- [轨迹记录](docs/TRACE_VALIDATION.md)、[音频验收](docs/AUDIO_VALIDATION.md)
- [历史状态与验证记录](docs/HISTORY.md)（重构前的 README，含历次测量）

## 数据与隐私

后台任务从登记起最多执行一分钟（排队计入，等待用户澄清暂停）。30秒仍未结束时主动同步一次真实阶段进度，正在对话/朗读则顺延到空闲；最后15秒停止扩展检索，使用已有资料收尾。阶段结果明确保留未核实项；超时仍未能交付则停止，来源留在任务卡。内部异常与原始搜索语句只供诊断，不进入默认播报；任务卡显示计时和阶段结果。旧环境配置即使仍为`THINKER_TIMEOUT_SECONDS=300`也会被硬限制为60秒。

轨迹和任务存放在本机 `data/super_xd.sqlite3`（WAL）。凭证字段、已知密钥值和 Bearer 头会被脱敏，但用户对话、模型输出和工具结果以明文保存。分享数据库或轨迹前请先检查。

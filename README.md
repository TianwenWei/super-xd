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
| `TAVILY_API_KEY` | 后台调研需要 | Thinker 的网页搜索与正文读取（tavily.com） |
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

2026-10-15 的结果（42 组用例×人设，各跑 3 次）：重构前 78/126，当前 125/126。差距主要来自旧代码把人设放进了决定工具调用的那次请求（违反不变量 6）。唯一残余失败是“先别开氛围灯”偶尔多发一条关闭指令（车况不变）。

带 `personas` 字段的用例会在每个人设下各跑一遍，并检查工具调用序列完全一致（不变量 6）。带 `maps` 标签的用例还需要 `GOOGLE_MAPS_API_KEY`。修 bug 时先在这里加失败用例，再按 AGENTS.md §6 修改。

## 文档

- [接口（WebSocket / HTTP）](docs/INTERFACES.md)
- [100 个车控函数与 10 个 skill](docs/TOOLS.md)
- [Thinker 设计](docs/THINKER.md)
- [轨迹记录](docs/TRACE_VALIDATION.md)、[音频验收](docs/AUDIO_VALIDATION.md)
- [历史状态与验证记录](docs/HISTORY.md)（重构前的 README，含历次测量）

## 数据与隐私

轨迹和任务存放在本机 `data/super_xd.sqlite3`（WAL）。凭证字段、已知密钥值和 Bearer 头会被脱敏，但用户对话、模型输出和工具结果以明文保存。分享数据库或轨迹前请先检查。

# 说明书轻量向量检索原型

Talker 通过固定的 `search_manual(query, max_results?)` 工具检索车书，检索结果交回同一个 Talker 组织自然口语回答。车书问答走现有异步 lookup 管线，不登记后台任务，不执行车控。当前向量库已完成建库，语音与文字请求使用相同工具与 prompt。

来源为腾势官网的[《腾势 D9 用户手册》中文右舵版](https://www.denza.com/material/tengshi-overseas-site/support/%E9%A8%B0%E5%8B%A2D9%E7%94%A8%E6%88%B6%E6%89%8B%E5%86%8A--%E5%8F%B3%E8%88%B5.pdf)，本地保存在 `output/pdf/腾势D9用户手册-中文右舵版.pdf`。这是快速模拟实验的资料版本，车型/地区配置需按原文核对。

## 前后端接口

`lookup.result` 保留现有协议：`{call_id, tool: "search_manual", result}`。成功 `result` 包含 `ok:true, kind:"manual", provider:"车书", query, evidence:"manual_text", manual:{id,title,pages,pdf_url}, hits:[{chunk_id,page,section,text,score,images:[{url,caption,page}]}], notice,image_notice,queried_at,embedding_ms,local_search_ms,cache_hit`。

- `manual.pdf_url` 为同源 `/api/manual/pdf`，以 `application/pdf` 内联返回原始车书；附加 `#page=<page>` 可打开对应页。
- `hits[].images[].url` 为同源 `/api/manual/<manual.id>/pages/<page>.png`，以 `image/png` 返回真实 PDF 页面的整页渲染图；不是生成图片，也不是逐图语义识别。`caption` 明确标注“原始 PDF 整页预览”。相关性来源于文字命中的同一页。
- 每页图先渲染成功、确认文件存在后才放入 `images`。同页重复命中复用同一 URL，前端按页去重展示。未安装 Poppler、源 PDF 缺失或渲染失败时，该页 `images:[]`，`image_notice` 明确提示预览不可用，可查看 PDF。
- 只允许返回当前已导入手册的 ID 和合法页码；未知手册或页码返回 404。图片缓存在 `data/manuals/pages/<id>/`，不提交 Git。渲染并发最多两页，取消请求时终止渲染子进程。
- 缺少向量密钥、缺失/未完成/不兼容索引、网络超时沿用 `lookup.result` 的 `ok:false` 错误路径；不得生成替代资料。

`GET /api/config` 的 `manual` 字段返回模型、密钥是否配置、索引状态、页数、片段数和标题，不返回密钥。服务启动在工作线程预载本地索引；同一问题最多缓存 32 个向量，后续调用复用，矩阵比对与 SQLite 读取不阻塞 actor。

采用 pypdf 在本地读取 PDF 的文字层与章节书签，按页切成最多 600 字、重叠 100 字的片段，保留页码和章节。只向火山方舟发送文字，不发送 PDF 页面图片，不使用 OCR。复杂表格及插图信息可能缺失。当前材料为腾势 D9 中文右舵版，不能据此认定大陆车型或模拟车辆具有相同配置。

每个片段通过 `doubao-embedding-vision-251215` 生成一个 1024 维向量；文档与问题分别使用官方推荐的 Corpus/Query 指令模板。注意同一多模态请求的 input 列表会合成一个向量，不可把多个独立片段塞进该列表当作批量向量化。脚本使用四路并发的独立请求。

原文、页码、章节和归一化 float32 向量保存在 `data/manuals/denza_d9.sqlite3`。查询仅将问题送入 embedding 模型，再用 NumPy 在本地执行余弦相似度排序。无需独立数据库服务；分数不是答案可信度，仍需回答模型判断原文是否足以回答问题。

## 配置与运行

安装可选依赖（项目根目录）：

```sh
uv sync --locked --extra demo --extra manual-index
```

项目 `.env` 配置以下变量；专用密钥只授权 `Doubao-embedding-vision`，与原有 `ARK_API_KEY` 分开：

```dotenv
ARK_EMBEDDING_API_KEY=<在火山方舟创建的专用密钥>
ARK_EMBEDDING_MODEL=doubao-embedding-vision-251215
ARK_EMBEDDING_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
```

```sh
.venv/bin/python -m scripts.manual_index build
.venv/bin/python -m scripts.manual_index query '车载冰箱的制冷温度可以调到多少度？'
```

`build` 可重复执行：已有的成功向量不会重算。未完成的索引拒绝查询。更换来源 PDF、切片配置、模型或 embedding 指令时，指定新的 `--db` 路径重新建库。`--pdf` 可指定其他 PDF，`query --top-k 3` 可调整返回数量。

查询 JSON 包含实际原文、页码、章节、余弦分数、embedding 延迟和本地检索延迟。未调用回答模型，不生成总结或语音。

## 本次验证

2026-10-15 建库结果：298 页、385 个文字片段、1024 维向量，SQLite 文件 2,113,536 字节。接口真实返回 HTTP 200，文字请求的图片 tokens 为 0。一次上游网络超时后已通过重跑恢复建库，最终没有缺失向量。

```sh
.venv/bin/python -m evals.manual_retrieval --repeat 3
```

五类问题（冰箱温度、无线充电、NFC 解锁、自动驻车条件、胎压报警）各重复三次，Top-5 来源页码命中 15/15。报告在 `data/evals/manual_retrieval.json`；embedding 延迟 P50 为 1890.52 ms，本地向量装载到矩阵与相似度排序 P50 为 0.49 ms，不包含 SQLite 读取或回答模型耗时。样本很小，仅用于原型验收，不代表任意问题都能回答。NFC 解锁的指定页出现在前三项，其余四类指定页位于第一项。

接入后的离线测试覆盖专用凭证隔离、纯文本请求、非法向量/未完成索引拒绝、错误脱敏、查询向量缓存、固定工具集、同一 Talker 的资料续答、打断后结果仅进 trace、无任务/车控副作用和非法图片页拒绝。前端使用 `ManualResults.tsx` 显示图片与原文；构建及 `lookup-views` 的五项 URL/去重/字段测试通过。

真实检索“车载冰箱的制冷温度范围是多少？”命中 PDF 第238、237页，embedding 889.94 ms、本地排序0.16 ms；成功渲染真实原始页图，URL 分别为 `/api/manual/bc60d1ce35862903/pages/238.png`、`/api/manual/bc60d1ce35862903/pages/237.png`。图片并未参与向量化，文字请求的 `image_tokens=0`。

HTTP 核验：第238页 `200 image/png`，PNG 文件签名正确；原始 PDF Range 请求 `206 application/pdf`，文件签名 `%PDF`；未知手册ID `404`。并已目视核对第238页原始图中的制冷范围为 -6℃～6℃。接口与连续问答的完整脱敏轨迹在本机 `data/evals/manual_qa_trace.json`，包括模型输入/输出、embedding 请求/响应和工具回执，向量不会进入前端/回答模型。

前端并行验证使用隔离8005/5175服务，真实无线充电检索的231、232、221、233四张去重整页PNG均加载成功，缩略图保持完整页比例，原图和PDF链接可打开；验证服务在完成后停止，不作为正式运行端口。

## 助手接入与回答边界

Talker 的固定工具集已增加 `search_manual`，总数23；由模型决定本轮是否需要资料。`SessionActor` 复用异步 lookup 执行和同一个 Talker 的工具续答，磁盘读取与 NumPy 排序通过 `asyncio.to_thread`。迟到结果只记 trace；检索材料作为不可信资料，不能成为执行指令，动作仍以用户授权和车端回执为准。

真实回答审阅发现：在追问另一对象的电量状态时，模型曾移用相似片段中车辆低压电池的处理办法。已补充通用的对象/条件约束及“原文不足以确认具体参数”的原生示例，但当前 Mini 模型仍偶尔对原文未明确支持的结论自行推断。失败轨迹原样保留于 `data/evals/manual_qa_unsupported_claim_trace.json`，没有隐藏或当成已修复。命中相关页和结构化评测通过都不等于答案事实验证；这是模拟实验原型，需保留原文供核对。

2026-10-15 全套行为评测每项重复三次：接入前142/174（81.61%），最终174/174（100%），无新增结构化失败。最终报告 `data/evals/manual_qa_verified.json`。全186轮首句可送TTS P50/P90 从830/2044 ms变为819/1376 ms，平均模型调用次数1.26→1.25；总响应P50/P90为1873/6559 ms。这里的首句可能是查询提示，不是完整答案；这些数值不包含ASR和首PCM。查询类48轮总响应P50/P90为3419/5660 ms，平均1.88次模型调用；供应商和缓存存在波动，不能据此保证线上加速。

后端完整测试1057项通过，随后补充的损坏元信息测试及相关51项检查通过。前端构建通过，车书URL/图片去重等5项测试通过。结构化评测只检查工具、页码和副作用；原文之外的推断风险见上面的真实失败轨迹。

最终话术审阅报告 `data/evals/manual_qa_verified.speech.json`：120条不同回复全部评分，自然度均值3.99/5；85条工具前话术没有提前宣布动作完成。审阅仅作参考，不验证事实。

正式本机8000后端已加载更新，`/api/health.manual` 显示 `configured=true, ready=true, pages=298, chunks=385`。前端仍为5173；后端重启后需在界面新建会话。用户于本次明确特别授权更新 `AGENTS.md` 的支持能力；已补充目录、环境变量、固定23工具和车书资料边界，未改动未来编辑权限条款。

## 旧版模型为何返回 404

火山官方的[模型下线公告](https://docs.volcengine.com/docs/ark/model-deprecation-notice?lang=zh)将 `doubao-embedding-large-text-250515` 和 `240915` 列入 2025-12-26 停止新购的模型：存量接入点可继续使用，但不能新增旧版服务。即使服务家族在“开通管理”显示已开通，也不代表新账号能够通过该旧版 Model ID 调用。官方替代型号为 `doubao-embedding-vision-251215`，其[文档](https://docs.volcengine.com/docs/ark/vectorization?lang=zh&redirect=1)支持纯文本输入。

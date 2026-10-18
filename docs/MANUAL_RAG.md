# 本地车书检索

Talker 通过固定的 `search_manual(query, max_results?)` 工具检索车书，检索结果交回同一个 Talker 组织自然口语回答。车书问答由NLPService发出只读资源请求，不登记后台任务、不执行车控。Demo通过ManualProvider返回实际检索结果；语音与文字请求使用相同工具与prompt。

来源为腾势官网的[《腾势 D9 用户手册》中文右舵版](https://www.denza.com/material/tengshi-overseas-site/support/%E9%A8%B0%E5%8B%A2D9%E7%94%A8%E6%88%B6%E6%89%8B%E5%86%8A--%E5%8F%B3%E8%88%B5.pdf)，默认导入路径为 `output/pdf/腾势D9用户手册-中文右舵版.pdf`，需自行准备实际PDF。该资料用于Demo检索，车型/地区配置需按原文核对。

## 前后端接口

Demo的`lookup.result`协议为：`{call_id, tool: "search_manual", result}`。成功 `result` 包含 `ok:true, kind:"manual", provider:"车书", query, evidence:"manual_text", manual:{id,title,pages,pdf_url}, hits:[{chunk_id,page,section,text,score,images:[{url,caption,page}]}], notice,image_notice,queried_at,embedding_ms,local_search_ms,cache_hit`。

- `manual.pdf_url` 为同源 `/api/manual/pdf`，以 `application/pdf` 内联返回原始车书；附加 `#page=<page>` 可打开对应页。
- `hits[].images[].url` 为同源 `/api/manual/<manual.id>/pages/<page>.png`，以 `image/png` 返回真实 PDF 页面的整页渲染图；不是生成图片，也不是逐图语义识别。`caption` 明确标注“原始 PDF 整页预览”。相关性来源于文字命中的同一页。
- 每页图先渲染成功、确认文件存在后才放入 `images`。同页重复命中复用同一 URL，前端按页去重展示。未安装 Poppler、源 PDF 缺失或渲染失败时，该页 `images:[]`，`image_notice` 明确提示预览不可用，可查看 PDF。
- 只允许返回当前已导入手册的 ID 和合法页码；未知手册或页码返回 404。图片缓存在 `data/manuals/pages/<id>/`，不提交 Git。渲染并发最多两页，取消请求时终止渲染子进程。
- 缺少向量密钥、缺失/未完成/不兼容索引、网络超时沿用 `lookup.result` 的 `ok:false` 错误路径；不得生成替代资料。

`GET /api/config` 的 `manual` 字段返回模型、密钥是否配置、索引状态、页数、片段数和标题，不返回密钥。服务启动在工作线程预载本地索引；同一问题最多缓存 32 个向量，后续调用复用，矩阵比对与 SQLite 读取不阻塞 actor。

采用 pypdf 在本地读取 PDF 的文字层与章节书签，按页切成最多 600 字、重叠 100 字的片段，保留页码和章节。只向选中的embedding服务发送文字，不发送PDF页面图片，不使用OCR。复杂表格及插图信息可能缺失。当前材料为腾势 D9 中文右舵版，不能据此认定大陆车型或模拟车辆具有相同配置。

默认Ark配置通过`doubao-embedding-vision-251215`为每个片段生成1024维向量；文档与问题分别使用官方推荐的 Corpus/Query 指令模板。注意同一多模态请求的 input 列表会合成一个向量，不可把多个独立片段塞进该列表当作批量向量化。脚本使用四路并发的独立请求。

原文、页码、章节和归一化 float32 向量保存在 `data/manuals/denza_d9.sqlite3`。查询仅将问题送入 embedding 模型，再用 NumPy 在本地执行余弦相似度排序。无需独立数据库服务；分数不是答案可信度，仍需回答模型判断原文是否足以回答问题。

## 配置与运行

安装可选依赖（项目根目录）：

```sh
uv sync --locked --extra demo --extra manual-index
```

默认Ark配置在本地`.env`或进程环境提供以下变量；embedding凭证与`ARK_API_KEY`独立。其他实现见[向量服务](EMBEDDING_PROVIDERS.md)，远端车书CP见[车书服务](MANUAL_PROVIDERS.md)：

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

## 助手接入与回答边界

`search_manual`属于Talker固定26工具，由模型决定何时读取资料。NLPService接收真实资源回执后继续同一个Talker调用链；Demo负责本地索引、PDF页图和界面展示，磁盘读取与NumPy排序通过`asyncio.to_thread`。迟到结果不能进入新轮输出；检索资料不能授予设备执行权限。

回答必须核对资料中的对象、车型、地区和条件；相似片段不能代替原文对具体结论的支持。余弦分数、命中页码和工具成功都不能证明答案正确。原文不足时明确说明，保留原文和页码供核对。

## 验证

```sh
.venv/bin/python -m evals.manual_retrieval --repeat 3
```

此命令验证当前索引的来源页码检索，不调用回答模型。报告保留实际原文、页码、embedding与本地检索耗时；模型回答质量、HTTP文件能力、浏览器图片和语音需分别验证。

离线回归覆盖专用凭证隔离、纯文本请求、非法向量/未完成索引拒绝、错误脱敏、缓存和合法页码。接口核验检查真实PDF、Range响应、PNG与原文对应、非法手册/页码拒绝；前端核对页图去重、原图及PDF链接。

历史真实问答审阅曾发现把低压电池片段移用于另一对象的电量问题；失败证据保留在本地`data/evals/manual_qa_unsupported_claim_trace.json`。该记录属于旧模型与Prompt，不能声明新版本已修复，也不能把旧版结构化通过率当作当前事实准确率。新版本按[验收方法](VALIDATION.md)重跑并抽查完整模型输入、资料回执与回答；报告和失败trace写入忽略的`data/evals/`或`data/validation/`，不提交Git。

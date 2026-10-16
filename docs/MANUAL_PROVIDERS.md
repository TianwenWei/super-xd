# 独立车书资源服务

车书问答通过 `ManualProvider` 读取资料。它与网页搜索、地点和媒体 CP 一样是独立资源服务；EmbeddingProvider 只负责本地 RAG 的文字向量，不能代替车书资源接口。模型、工具路由和前端不需要知道资料是本地检索还是远端 CP 返回。

## 切换提供商

`config/services.toml` 中 `[manual]` 选择 profile。`local_rag` 保留本地只读 SQLite 索引、实际 PDF 与整页渲染，向量由独立 `[embedding]` profile 提供。更换向量 provider、模型、维度或 Corpus/Query 指令必须重建索引，参见 [EMBEDDING_PROVIDERS.md](EMBEDDING_PROVIDERS.md)。

`http_gateway` 使用独立 `base_url`/`base_url_env`、`key_env` 和 `timeout_seconds`。默认密钥变量为 `MANUAL_API_KEY`；不会回退到 LLM 或 embedding 密钥。明确允许免鉴权的自有服务可配置 `require_api_key = false`。缺少必要配置返回 `not_configured`，不会改走本地索引。

## 远端 CP HTTP 契约

请求为 `POST <base_url>/v1/manual/search`，配置密钥时使用 Bearer Authorization。JSON 请求：

```json
{"query":"车窗如何使用","max_results":5}
```

`query` 为 1–800 字符，`max_results` 为 1–5。响应示例中的资料必须由真实 CP 返回：

```json
{
  "ok":true,
  "manual":{"id":"manual-v1","title":"实际车型资料","pages":120,"source_url":"https://manual.example/original"},
  "hits":[{"chunk_id":"window-1","manual_id":"manual-v1","section":"车窗","page":12,"text":"实际原文片段","source_url":"https://manual.example/original#page12","images":[]}],
  "queried_at":"2026-10-16T12:00:00+00:00"
}
```

有片段时必须声明文档身份、标题、总页数。每个片段必须有唯一 `chunk_id`、真实章节、原文和有效页码；可选 `score` 只能是有限数值，仅用于排序。出处可以来自片段 `source_url`、文档 `pdf_url`/`source_url`，或 `sources:[{id,title,url}]` 与片段 `source_id` 的真实关联。链接必须是无 userinfo 的 HTTP(S) URL。不能用本地文件路径或虚构的 `/api/manual/...` 路径代替远端出处。

`images` 可省略或为 `[]`。CP 提供图片时每项必须包含实际 `url` 和与片段相同的 `page`；适配器原样保留，不下载、渲染或伪造图片。没有匹配结果可返回 `{"ok":true,"hits":[]}`，无需编造文档。

## 可用性、文件能力和审计

`prepare()` 只检查配置，不发送隐式查询，也不宣称远端已经验证可用。只有真实检索成功后状态才标记为 `query_succeeded`。超时、鉴权、不可用和无效响应分别返回统一 `ProviderError` code；实际请求与响应进入脱敏 `_provider_trace`。

远端 CP 的出处链接由前端直接展示。当前远端适配器没有本地 `document()` 或 `page_image()` 文件能力，这两项明确返回 `unsupported_capability`；不会拿本地 D9 PDF 冒充远端资料。`with_page_images()` 仅保留 CP 已返回的真实图片。已有本地 RAG 继续提供真实 PDF 与原始整页图。

契约测试：`.venv/bin/python -m pytest tests/test_manual_providers.py tests/test_manual_search.py tests/test_embedding_providers.py`。MockTransport 覆盖真实 HTTP 字段、来源校验、缺配置和脱敏；测试不代表已连接任何尚未配置的真实车书 CP。

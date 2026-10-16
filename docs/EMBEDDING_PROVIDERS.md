# 车书向量服务

服务选择位于 `config/services.toml` 的 `embedding` 段。默认 `ark` 保留现有独立 `ARK_EMBEDDING_API_KEY`、火山 multimodal 请求结构、Corpus/Query 指令与1024维索引；不会借用 Talker 的 API Key。

`backend/services/embedding.py` 提供 `build_embedding(profile)`，结果统一支持 `embed(text, query=False)`、`model`、`dimensions`、`provider`、`info()` 和 `aclose()`。返回真实向量、model、provider、usage、耗时和脱敏后的实际请求/响应 trace。

## OpenAI 兼容接口

选择已声明的 `compatible` profile（或设置 `EMBEDDING_PROFILE=compatible`），配置 `EMBEDDING_BASE_URL`（含 `/v1`）、`EMBEDDING_MODEL`、独立 `EMBEDDING_API_KEY`，并把 `dimensions` 设置为服务实际支持的维度。

适配器发送 `POST <base_url>/embeddings`：

```json
{"model":"配置的模型名","dimensions":1024,"encoding_format":"float","input":"Corpus或Query指令\n\n原文或问题"}
```

标准接口没有火山的独立 `instructions` 字段，因此把相应指令作为真实 input 的前缀。Corpus 与 Query 使用各自的现有指令，配置修改后需要重建索引。

只接受单个真实向量，维度必须完全一致，每个值必须是有限数值，向量非零且可归一化；响应 model 必须与配置的索引 model 一致。Compatible 维度范围为1到65536，并不意味着提供商支持这个范围内的每个值。Ark 包装器只接受原服务支持的1024/2048维。缺配置、缺必需密钥或响应无效时明确报错，不生成替代向量。

## 索引身份与重建

新索引 metadata 保存 `embedding_provider`、`embedding_model`、`dimensions`、Corpus/Query 指令及来源。旧索引没有 `embedding_provider` 时只按 `ark` 处理。

更换 provider、model、维度或指令后，运行 `python -m scripts.manual_index build --db <新的索引路径>` 重建，配置 `MANUAL_INDEX_PATH` 指向新索引，再重启 API/Worker。运行服务不会拿不同提供商的新问题向量与旧索引混用；未完成、非有限或零向量索引同样拒绝查询。

契约测试使用显式 HTTP fixtures，不代表已连接用户的新向量服务或验证其检索质量。`test_ark_wrapper_preserves_original_request_and_normalization` 对照默认 Ark 请求正文和归一化结果，确保包装本身没有改变原调用。

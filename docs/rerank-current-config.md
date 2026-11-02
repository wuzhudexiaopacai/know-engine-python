# 当前重排（Rerank）模型配置现状

> 采集时间：2026-11-01
> 采集方式：直接读取 MySQL `model_role_binding` / `llm_model` 并调用 `resolve_role_runtime("rerank")` 实测

---

## 一、当前生效的重排模型

| 项 | 值 |
|----|----|
| 用途绑定 | `model_role_binding.role = rerank` → `model_id = 11` |
| 模型记录 | `llm_model` id=11，展示名「本地 Qwen2.5 3B」 |
| 上游模型名 | `qwen2.5:3b-instruct` |
| 提供方 | `provider = local` |
| 是否启用 | `enabled = 1` |
| API Key | 空（`allow_empty_key = true`，本地服务不校验密钥） |
| 配置地址（base_url） | `http://127.0.0.1:8080` |
| **完整请求地址** | `http://127.0.0.1:8080/rerank` |

说明：`base_url` 只填了根地址，[rerank_endpoint()](../app/services/rerank.py#L51-L70) 在调用时自动补 `/rerank`，得到完整端点。

同一用途下另有一条候选模型记录：`llm_model` id=10「本地 bge-reranker-v2-m3」，`model_name = bge-reranker-v2-m3`，`base_url = http://127.0.0.1:8080`，但**未被绑定**为当前生效模型。

---

## 二、报文地址（请求地址与报文结构）

### 2.1 请求地址

- **服务根地址**：`http://127.0.0.1:8080`
- **完整报文地址**：`http://127.0.0.1:8080/rerank`
- **HTTP 方法**：`POST`
- **鉴权**：无（本地服务，不发送 `Authorization` 头）

### 2.2 实际请求体（本地协议，Jina 风格）

当前端点路径不含 `/api/v1/services/rerank`，按 [build_rerank_body()](../app/services/rerank.py#L73-L90) 判定为「非阿里原生协议」，输出顶层字段：

```json
{
  "model": "qwen2.5:3b-instruct",
  "query": "用户问题",
  "documents": ["切片1", "切片2", "……"],
  "top_n": 5
}
```

字段来源：

| 字段 | 来源 |
|------|------|
| `model` | 绑定模型的上游模型名 `qwen2.5:3b-instruct` |
| `query` | 检索改写后的用户问题 |
| `documents` | 混合召回候选切片，**单条截断 1000 字符、最多 20 条**（`_MAX_DOC_CHARS` / `_MAX_DOCS`） |
| `top_n` | `vector.reranked_top_k`（默认 8），调用时由检索层传入 |

### 2.3 响应解析

[parse_rerank()](../app/services/rerank.py#L93-L114) 兼容两种结构：

```json
// 本地 llama.cpp / vLLM / Infinity / TEI
[{"index": 0, "score": 0.91}, {"index": 2, "score": 0.32}]
```
```json
// 阿里原生：output 包裹，分数字段为 relevance_score
{"output": {"results": [{"index": 0, "relevance_score": 0.87}]}}
```

解析结果统一为 `[(原始下标, 分数)]`，供检索层重排并截断到 `reranked_top_k`。

### 2.4 失败降级

请求失败 / 超时（10s）/ 连续 3 次熔断（恢复 60s）时，`rerank()` 返回 `None`，检索层自动降级为 **RRF 融合排序**（多路召回排名投票），不影响回答链路。

---

## 三、环境变量兜底配置（当前未生效）

`model_role_binding` 已绑定模型，所以以下 `.env` 配置**不会生效**，仅作为兜底存在：

| 配置项 | 当前值 |
|--------|--------|
| `RERANK__BASE_URL` | `https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` |
| `RERANK__MODEL` | `gte-rerank-v2` |
| `RERANK__TOP_N` | `5` |
| `RERANK__TIMEOUT_SECONDS` | `10` |
| `RERANK__ENABLED` | `true` |

> 未配置 API Key 时，若绑定记录被移除，环境变量兜底也会因缺 Key 而跳过精排（直接 RRF）。

---

## 四、调用链

```
retrieval.rerank_aggregate()
   └── get_rerank().rerank(query, documents, top_n)          # app/services/rerank.py
       ├── 截断：documents[:20]，每条 [:1000]
       ├── 解析生效模型：resolve_role_runtime("rerank")
       │     ├── 命中绑定 → llm_model id=11（qwen2.5:3b-instruct @ 127.0.0.1:8080）
       │     └── 未绑定 → RERANK__ 环境变量（gte-rerank-v2 @ dashscope）
       ├── rerank_endpoint(base_url) → 补全完整地址
       ├── build_rerank_body(url, model, query, docs, top_n)
       ├── POST（失败重试 2 次，超时/熔断降级）
       └── parse_rerank(resp) → [(index, score)] 降序
```

---

## 五、与阿里接口的差异（适配前的关键事实）

| 对比项 | 当前本地模型 | 阿里原生 Rerank（含专属 MaaS） |
|--------|--------------|------------------------------|
| 端点路径 | `/rerank` | `/api/v1/services/rerank/text-rerank/text-rerank` |
| 报文结构 | 顶层 `query` / `documents` / `top_n` | `input.{query,documents}` + `parameters.{top_n,instruct}` |
| 鉴权 | 无 | `Authorization: Bearer $DASHSCOPE_API_KEY` |
| 响应 | 顶层数组或 `results` | `output.results[].relevance_score` |
| 协议判定 | 代码原按 `"dashscope" in url` 判断 | 专属 MaaS 域名 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` **不含 dashscope**，被误判 |

最后一行就是"看起来不兼容阿里"的直接原因，适配方案见 [rerank-aliyun-adaptation.md](./rerank-aliyun-adaptation.md)。

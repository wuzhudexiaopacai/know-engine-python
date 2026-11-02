# 重排模型阿里（DashScope / 专属 MaaS）适配结果

> 完成时间：2026-11-01
> 关联文档：[当前重排模型配置现状](./rerank-current-config.md)

---

## 一、适配目标

让系统同时兼容阿里重排的两类地址：

1. **DashScope 公有云原生协议**
   `https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`
2. **专属 MaaS 工作空间域名**
   `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`

并顺带支持百炼 OpenAI 兼容端点 `https://dashscope.aliyuncs.com/compatible-mode/v1/rerank`。

---

## 二、根因

适配前，[build_rerank_body()](../app/services/rerank.py) 用**域名关键字**判断协议：

```python
if "dashscope" in url:          # 旧逻辑
    return {"model": ..., "input": {...}, "parameters": {...}}
return {"model": ..., "query": ..., "documents": ..., "top_n": ...}
```

专属 MaaS 域名 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` **不含 `dashscope`**，因此被误判为 Jina 风格，发出顶层 `query/documents/top_n` 报文；阿里原生接口只接受 `input` + `parameters` 包裹，返回 400 → 系统降级为 RRF，表现为「重排不起作用 / 不兼容阿里」。

---

## 三、改动清单

| # | 文件 | 改动 | 作用 |
|---|------|------|------|
| 1 | [rerank.py](../app/services/rerank.py#L41-L48) | 新增 `is_dashscope_native()` / `is_aliyun_endpoint()` | **按 URL 路径 `/api/v1/services/rerank` 判定协议**，不再依赖域名关键字 |
| 2 | [rerank.py](../app/services/rerank.py#L51-L70) | `rerank_endpoint()` 增加阿里端点补全规则 | 用户只填域名或根路径时自动补成可用地址 |
| 3 | [rerank.py](../app/services/rerank.py#L73-L90) | `build_rerank_body()` 改用路径判定 | MaaS 域名正确生成 `input`/`parameters` 报文 |
| 4 | [rerank.py](../app/services/rerank.py#L181-L186) | TEI `texts` 兜底排除阿里端点 | 阿里 400 是参数/鉴权问题，改字段重试只会掩盖真实错误 |
| 5 | [service.py](../app/modules/model/service.py#L516-L519) | 「测试连接」同步排除阿里端点 | 保证模型配置页的连通性测试与真实调用一致 |
| 6 | [settings.py](../app/config/settings.py#L143-L144) | 新增 `RERANK__INSTRUCT`（默认空） | qwen3-rerank 等模型可携带检索意图指令 |

### 3.1 端点补全规则（`rerank_endpoint`）

| 用户填写 | 实际请求地址 |
|----------|--------------|
| `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` | 原样使用 |
| `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com` | `…/api/v1/services/rerank/text-rerank/text-rerank` |
| `https://dashscope.aliyuncs.com` | `…/api/v1/services/rerank/text-rerank/text-rerank` |
| `https://dashscope.aliyuncs.com/api/v1` | `…/api/v1/services/rerank/text-rerank/text-rerank` |
| `https://dashscope.aliyuncs.com/compatible-mode/v1` | `…/compatible-mode/v1/rerank` |
| `https://dashscope.aliyuncs.com/compatible-mode/v1/rerank` | 原样使用 |
| `http://127.0.0.1:8080` | `http://127.0.0.1:8080/rerank`（本地逻辑不变） |
| `http://localhost:8080/rerank` | 原样使用（本地逻辑不变） |

### 3.2 协议与报文

判定为阿里原生时（路径含 `/api/v1/services/rerank`）：

```json
{
  "model": "qwen3-rerank",
  "input": {
    "query": "什么是文本排序模型",
    "documents": ["文本排序模型广泛用于搜索引擎和推荐系统中", "……"]
  },
  "parameters": {
    "top_n": 8,
    "return_documents": false,
    "instruct": "Given a web search query, retrieve relevant passages that answer the query."
  }
}
```

- `instruct` 仅在配置了 `RERANK__INSTRUCT` 时出现；默认不传，不影响旧模型。
- 鉴权头：`Authorization: Bearer <模型配置中的 api_key>`。
- 其余端点（本地 / Jina / vLLM / OpenAI 兼容）报文完全不变。

---

## 四、验证结果

### 4.1 端点与协议映射（代码实测）

| 输入 base_url | 补全结果 | 判定协议 |
|---------------|----------|----------|
| `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` | 原样 | 阿里原生 ✅ |
| `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com` | `…/api/v1/services/rerank/text-rerank/text-rerank` | 阿里原生 ✅ |
| `https://dashscope.aliyuncs.com` | `…/api/v1/services/rerank/text-rerank/text-rerank` | 阿里原生 ✅ |
| `https://dashscope.aliyuncs.com/api/v1` | `…/api/v1/services/rerank/text-rerank/text-rerank` | 阿里原生 ✅ |
| `https://dashscope.aliyuncs.com/compatible-mode/v1` | `…/compatible-mode/v1/rerank` | 顶层字段 ✅ |
| `https://dashscope.aliyuncs.com/compatible-mode/v1/rerank` | 原样 | 顶层字段 ✅ |
| `http://127.0.0.1:8080` | `http://127.0.0.1:8080/rerank` | 顶层字段（本地） ✅ |
| `http://localhost:8080/rerank` | 原样 | 顶层字段（本地） ✅ |

### 4.2 真实请求（DashScope 公有云，使用后台已配置的阿里 Key）

```text
POST https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank

model=gte-rerank-v2  → HTTP 200
output.results = [{index: 0, relevance_score: 0.8756}, {index: 2, relevance_score: 0.3410}]
解析结果       = [(0, 0.8756), (2, 0.3410)]

model=qwen3-rerank   → HTTP 200
output.results = [{index: 0, relevance_score: 0.8285}, {index: 2, relevance_score: 0.7439}]
解析结果       = [(0, 0.8285), (2, 0.7439)]
```

两种模型均返回 200 且分数被正确解析，说明原生报文与响应解析链路已打通。

> 专属 MaaS 端点使用用户自己的 `DASHSCOPE_API_KEY` 与 WorkspaceId，报文格式与公有云完全一致，无需额外适配。

---

## 五、接入步骤（阿里重排）

1. 进入「模型配置 → 新增模型」：
   - 用途：`rerank`（检索精排）
   - 提供方：`aliyun`
   - 模型名：`qwen3-rerank`（或 `gte-rerank-v2`）
   - API 地址：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com`
     或直接粘贴完整地址 `…/api/v1/services/rerank/text-rerank/text-rerank`
   - API Key：阿里百炼密钥
2. 点「测试连接」，应返回「精排正常」。
3. 在「模型配置 → 模型用途 → 检索精排」中把该模型绑定为当前精排模型。
4. （可选）`.env` 增加检索指令以提升 qwen3-rerank 效果：

```dotenv
RERANK__INSTRUCT=Given a web search query, retrieve relevant passages that answer the query.
```

---

## 六、注意事项

- **条数上限**：系统送精排前统一截断为最多 20 条、单条 1000 字符（`_MAX_DOCS` / `_MAX_DOC_CHARS`），阿里接口限制更宽，无冲突。
- **超时**：默认 10 秒；云端网络慢可调 `RERANK__TIMEOUT_SECONDS`。
- **失败即降级**：任何异常（网络、鉴权、熔断）都会返回 `None` 并自动走 RRF，不会让问答 500。
- **本地模型零影响**：`http://127.0.0.1:8080` 仍按 `/rerank` + 顶层字段调用；TEI 的 `texts` 兜底仍只对非阿里端点生效。

### 6.1 常见报错

**`HTTP 400: {"code":"BadRequest.IllegalInput","message":"The input parameter requires json format."}`**

不是协议适配问题，而是**模型的「用途」选错了**。典型场景：新建模型时用途选了「文本向量化（embedding）」，测试连接会请求 `{base_url}/embeddings` 并发送 `{"model":..., "input": ["ping"]}`；阿里网关匹配到 rerank 处理器后，发现 `input` 是数组而不是 JSON 对象，于是返回上述 400。

处理方式：

1. 把该模型的用途改为 **「检索精排（rerank）」**（地址已经是 rerank 端点，无需改动）。
2. 重新点「测试连接」，应返回「精排正常」。

已加入防呆：若 `base_url` 含 `/services/rerank` 而用途不含 rerank，测试会直接提示
「该地址是重排（rerank）端点，请把「用途」改为「检索精排」后再测试」，不再发出必错的请求。

实测（专属 MaaS 地址 `https://llm-oj58e0xqimr13y1c.cn-beijing.maas.aliyuncs.com/…`，模型 `qwen3.7-text-rerank`）：
用途改为 rerank 后测试结果 `ok=True, "精排正常"`，返回
`{"output":{"results":[{"index":0,"relevance_score":0.6886}]}}`，分数解析正常。

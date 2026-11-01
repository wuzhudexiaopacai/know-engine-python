# 本地精排（Rerank）部署 + RAG 检索修复 · README

> 本文记录两件事：
> 1. 在**完全本地、纯 CPU** 上部署一个中文精排模型（bge-reranker-v2-m3），以及从下载到配置、启动、验证的全流程；
> 2. 期间排查并修复的所有 RAG 检索问题——每个问题都按「现象 / 根因 / 修复 / 修复前后对比」说明。
>
> 环境：Windows · Python 3.12 · Elasticsearch 8（Docker）· MySQL 8 · Ollama（本地对话/向量模型）

---

## 一、为什么需要「精排」

RAG 检索是两段式，精排是第二段：

```
问题 ──> 向量召回 top 30（快，但只是"大概相关"）
            │
            └──> 精排 Rerank（慢，但"真正看懂"问题和文档的关系）
                     │
                     └──> 只留最相关的 3~8 条 ──> 交给大模型生成回答
```

- **召回模型（Embedding，双塔）**：问题和文档分别编码成向量再比余弦相似度。文档向量是离线算好的，所以召回很快，但它看不到「问题 + 文档」的联合语义，只能判断"这两段话在说同一类东西"。
- **精排模型（Rerank，Cross-Encoder）**：把问题和文档**成对**送进同一个 Transformer，直接输出一个相关性分数。精度高很多，但每对都要推理一次，所以只能用在召回后的少量候选上。

没有精排时，召回排序里经常出现「关键词对上了、意思不对」的结果，这些片段被塞进 Prompt 后，模型就会答非所问。

---

## 二、为什么不能用 Ollama 跑精排

Ollama 只提供两类接口：

| 接口 | 用途 |
| --- | --- |
| `/api/chat`、`/api/generate` | 对话 / 续写 |
| `/api/embed`、`/api/embeddings` | 向量化（双塔） |

**它没有 `/rerank` 接口**，也没有加载 Cross-Encoder 分类头的能力。在 Ollama 里 `embed` 一个 reranker 模型，得到的仍然是向量，不是相关性分数。

可以查到的「ollama 上的 rerank 模型」（如 `bge-reranker`）实际都只能当 embedding 用，等于退化成了召回模型。

> 结论：本地精排要么用 `llama.cpp --reranking`，要么用 TEI / Infinity / vLLM。本项目选 **llama.cpp**，原因是它在 Windows 上有现成二进制、CPU 就能跑、内存占用小。

---

## 三、模型选型

| 模型 | 体积（Q8_0） | 结论 |
| --- | --- | --- |
| **bge-reranker-v2-m3** | 约 606 MB | ✅ 采用。中英双语、中文长文本效果好、0.57B 参数，CPU 可跑 |
| Qwen3-Reranker-0.6B | 约 640 MB | ❌ 放弃。GGUF 下载受阻（见下方「踩坑记录」） |
| gte-rerank-v2（线上 API） | — | 作为兜底保留，需阿里云百炼 Key |

### 踩坑记录：Qwen3-Reranker-0.6B 为什么没下成

- `huggingface.co` 直连超时；
- 换 `hf-mirror.com` 镜像后该仓库返回 **401**；
- ModelScope 上该仓库只有 `fp16` 的 safetensors，**没有 GGUF**，llama.cpp 不能直接加载；
- 自己转 GGUF 需要额外装转换工具链，收益不划算。

最终改用 **ModelScope 的 `gpustack/bge-reranker-v2-m3-GGUF`**，直接有 Q8_0 量化版。

---

## 四、下载与安装全流程

### 步骤 1：安装 llama.cpp（Windows）

```powershell
winget install ggml.llamacpp
```

> ⚠️ 包名必须是 **`ggml.llamacpp`**。写成 `ggml-org.llama.cpp` 会报 `No package found`。
> 本次安装的是 build **b12019**（`llama-server --version` 可查看）。

装完后 `llama-server` **不一定立刻在 PATH 里**（winget 只做了 alias 链接，当前终端未刷新）。此时直接报：

```
llama-server : 无法将"llama-server"项识别为 cmdlet、函数、脚本文件或可运行程序的名称
```

两种解法：
1. 重开一个终端（PATH 刷新）；
2. 或直接用完整路径（推荐写进启动脚本，避免依赖 PATH）：

```
C:\Users\ndx\AppData\Local\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-server.exe
```

### 步骤 2：下载模型

**方式 A：ModelScope 网页下载（最稳）**

打开 `https://modelscope.cn/models/gpustack/bge-reranker-v2-m3-GGUF`，
进入「模型文件」页，下载 `bge-reranker-v2-m3-Q8_0.gguf`（约 606 MB），
放到 `D:\models\rerank\` 目录。

**方式 B：命令行**

```powershell
pip install modelscope
modelscope download --model gpustack/bge-reranker-v2-m3-GGUF bge-reranker-v2-m3-Q8_0.gguf --local_dir D:\models\rerank
```

**方式 C：HuggingFace（国内大概率失败）**

```powershell
pip install "huggingface_hub[cli]"
hf download gpustack/bge-reranker-v2-m3-GGUF bge-reranker-v2-m3-Q8_0.gguf --local-dir D:\models\rerank
```

下载完成后确认：

```powershell
Get-ChildItem D:\models\rerank
# bge-reranker-v2-m3-Q8_0.gguf   606.20 MB
```

### 步骤 3：启动服务

创建 `D:\models\rerank\start-rerank.ps1`：

```powershell
# 启动本地精排服务（bge-reranker-v2-m3 Q8_0，CPU）
# 知识引擎「模型配置 → 检索精排」对应的服务端点：http://127.0.0.1:8080
$exe = "C:\Users\ndx\AppData\Local\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-server.exe"

# -c 8192：bge-reranker-v2-m3 原生上下文
# -b / --ubatch-size 必须 > 单条(query+doc)的 token 数；
# 默认 ubatch=512 会让超过约 700 字的文档直接 500，这里放宽到 4096
& $exe -m "D:\models\rerank\bge-reranker-v2-m3-Q8_0.gguf" `
       --reranking --port 8080 -c 8192 -b 4096 --ubatch-size 4096 --log-disable
```

运行：

```powershell
& D:\models\rerank\start-rerank.ps1
```

参数逐个说明：

| 参数 | 作用 |
| --- | --- |
| `-m` | GGUF 模型路径 |
| `--reranking` | **关键**：以重排模式启动，暴露 `/rerank`；不加就是普通对话服务 |
| `--port 8080` | 服务端口（不要和 Ollama 的 11434、ES 的 9200 冲突） |
| `-c 8192` | 上下文长度 |
| `-b 4096` | 批量大小 |
| `--ubatch-size 4096` | **关键**：单批 token 上限，太小会让长文档 500 |
| `--log-disable` | 关掉逐条请求日志（可选） |

### 步骤 4：验证服务

```powershell
# 健康检查
Invoke-RestMethod http://127.0.0.1:8080/health

# 相关性打分
$body = @{
  model = 'bge-reranker-v2-m3'
  query = 'Redis 持久化'
  documents = @('RDB 快照持久化', 'AOF 日志持久化')
  top_n = 2
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8080/rerank -Method Post -Body $body -ContentType 'application/json'
```

正常返回：

```json
{
  "model": "bge-reranker-v2-m3",
  "object": "list",
  "usage": { "prompt_tokens": 30, "total_tokens": 30 },
  "results": [
    { "index": 0, "relevance_score": 0.2245 },
    { "index": 1, "relevance_score": -4.3372 }
  ]
}
```

> **重要**：llama.cpp 返回的是**原始 logits**，可以为负数，不是 0~1 的概率。
> 本项目 `VECTOR__MIN_SCORE`（默认 `0.35`）会直接和这个分数比较，约等于 `sigmoid(0.35) ≈ 0.59` 的概率。
> 换别的精排模型时，这个阈值必须按新模型的分数分布重新调。

---

## 五、后台怎么配置

打开后台「模型配置」页（`/admin/model`），分两步：

### 第一步：录入模型

点「新增模型」，填写：

| 字段 | 填什么 |
| --- | --- |
| 用途 | **检索精排**（`rerank`） |
| 显示名称 | 本地 bge-reranker-v2-m3 |
| 模型名 | `bge-reranker-v2-m3` |
| 服务地址 | `http://127.0.0.1:8080` |
| API Key | **留空**（本地服务不校验鉴权） |
| 启用 | 开 |

> 服务地址只填根地址即可，代码会自动补 `/rerank`。
> 本地回环地址（`127.0.0.1` / `localhost`）会被识别为本地端点，自动允许空 Key。

### 第二步：绑定用途

在页面上方「模型用途」区块，找到 **检索精排**，下拉选择刚录入的模型。

- 留空 = 回退到「同用途的默认模型」或环境变量配置；
- 绑定后，检索链路的精排就走这个本地服务。

对应实现见：

- 用途定义：[service.py](file:///d:/code/rag/know-engine-python/app/modules/model/service.py) 中的 `ROLE_SPECS`
- 绑定表：`model_role_binding`（`role` → `model_id`）
- 本地端点判定：`_is_local_endpoint()`（provider=local 或地址含 localhost/127.0.0.1 → 免 Key）

---

## 六、端口分工（最容易搞混的地方）

| 端口 | 服务 | 能做什么 | 不能做什么 |
| --- | --- | --- | --- |
| 11434 | Ollama | 对话、向量化（`embeddinggemma-2`） | 不能精排（无 `/rerank`） |
| **8080** | llama.cpp（本机） | **只能精排** `/rerank` | 不能当对话模型 |
| 9200 | Elasticsearch | KNN / 全文 / 混合检索 | — |
| 8009 | 本项目后端 | API + SSE | — |

**典型错误**：把 `8080` 填给「对话模型」，聊天时报：

```
HTTP 500: the current context does not support logits computation
```

原因：`--reranking` 模式加载的是分类头，不是因果语言模型，无法做 token 预测。

---

## 七、故障排查速查表

| 现象 | 根因 | 解决 |
| --- | --- | --- |
| `401 InvalidApiKey / No API-key provided` | 地址没有 `rerank` 字样时，旧代码会回退到百炼 DashScope 地址；本地模型当然没有它家的 Key | 已修复：配哪个地址打哪个，不再回退；本地端点免 Key |
| `llama-server` 找不到 | winget 的 alias 未刷新 / 包名装错 | 用完整 exe 路径；包名 `ggml.llamacpp` |
| 精排 `500`，短文本正常、长文本失败 | `--ubatch-size` 默认 512，单条文档超过约 700 字就溢出 | 启动加 `-b 4096 --ubatch-size 4096`；代码侧同步截断单条 1000 字、最多 20 条 |
| `500 the current context does not support logits computation` | 把 8080 当对话模型用 | 对话模型指向 Ollama 11434，8080 只留给精排 |
| 检索结果全是无关内容 | 没有精排时靠词面命中，虚词/通用词被当成证据 | 已修复：见下文「问题 3」 |
| 聊天页提示「检索暂时不可用，已按通用知识回答」 | 向量维度与 ES 索引维度不一致，KNN 返回 400 | 已修复：见下文「问题 6」 |

---

## 八、已解决的 RAG 问题（现象 → 根因 → 修复 → 效果）

### 问题 1：切片标题丢失 / 编号标题不识别

**之前的现象**

- 引用卡片上的「来源标题」经常是空的，或者干脆是文档里的「整体总结（你可以直接背）」这种正文小标题；
- PDF / Word 转出来的 Markdown 没有 `#` 标记，于是整篇正文被当成一个章节，最后切出一个**几万字的父分段**。

**根因**

- 只用 `^#{1,6}\s+` 识别标题，不认 `第X章`、`1.`、`1.1`、`一、` 这类编号标题；
- 父分段没有大小上限。

**修复**

- [splitters.py](file:///d:/code/rag/know-engine-python/app/modules/document/splitters.py) 新增 `_NUMBERED_RE`，支持 `第X章/节/部分/篇/步`、`一、`、`1.1`、`1. ` 等编号标题；
- 维护标题栈，生成层级路径：`Redis > Redis的RDB和AOF区别`；
- 无标题时回退文档标题，不再出现空标题；
- 父分段封顶 `parent_max_size = max(chunk_size * 5, 3000)`，超长章节继续切分。

**现在的效果**

> 实测引用标题：`Redis > Redis的RDB和AOF区别`、`MySQL > 索引失效的情况`

---

### 问题 2：展示的「相似度」根本不是相似度

**之前的现象**

界面上每条引用都有「相似度 0.03、0.02」，看着像一回事，其实没有任何参考价值。

**根因**

RRF（倒数排名融合）算出来的是**排名分**（`1/(60+rank)` 量级约 0.01~0.03），被直接当成相似度展示。

**修复**

- `SearchHit` 增加 `vector_score`，保留 ES KNN 返回的**真实余弦相似度**；
- 精排成功后写入 `rerank_score`（真实相关性分数），不再用排名分冒充；
- 检索链路统一改为「有真实分就用真实分」。

**现在的效果**

引用分数有明确含义：向量相似度或精排相关性，阈值过滤才有意义。

---

### 问题 3：没有精排时疯狂误召回（最影响体验的一个）

**之前的现象**

问「倒车入库的操作流程是什么」，知识库里明明没有驾驶相关文档，却返回 8 条引用：

- JDK 安装流程
- 分布式事务实现
- Elasticsearch bulk 操作
- ……

分数还有 0.36 / 0.27，看起来"挺相关"。

**根因**

无精排时用「问题词在片段中的命中率」做校验，但分词把下面这些词也算成了证据：

- 疑问虚词：「的」「什」「么」「怎」「哪」
- 通用过程词：「操作」「流程」「步骤」「方法」「实现」「原理」

任何一篇技术文档都大量出现这些词，于是随便什么片段都能凑到 0.25 以上。

**修复**（[vector_store.py](file:///d:/code/rag/know-engine-python/app/infra/vector_store.py)）

1. `_STOP_CHARS`：只保留真正无实义的虚字（注意**不能**放「能/可/要/有/为」，否则会吞掉「性能/可靠/重要/有效」这类关键术语）；
2. `_GENERIC_TERMS`：约 50 个通用过程词，不参与相关性判定；
3. `query_terms()`：剔除虚词二元组、丢弃单字中文；
4. `term_overlap()`：**加权命中率**——英文/数字词权重 3，中文二元组权重 1；
5. `has_anchor()`：新增硬门槛——问题中必须有 **≥3 个连续汉字**（且其中不含虚词/通用词）原文出现在片段里；
6. 阈值从 `0.25` 提到 `0.4`（见 [retrieval.py](file:///d:/code/rag/know-engine-python/app/modules/rag/retrieval.py) 的 `_MIN_TERM_OVERLAP`）。

**现在的效果**

| 提问 | 之前 | 现在 |
| --- | --- | --- |
| 倒车入库的操作流程是什么 | 8 条无关引用 | **0 条**（明确告诉用户"未检索到相关资料"） |
| 分布式事务怎么实现 | 混入无关片段 | 8 条，相关度高 |
| MySQL 索引失效的情景 | 混杂 | 2 条，全部正确 |

> 设计取舍：宁可返回空结果并提示"未检索到相关资料"，也不给用户误导性的引用。

---

### 问题 4：召回数量与上下文预算失控

**之前的现象**

- 初召回条数偏少，好结果进不了候选；
- 一旦命中父分段，就可能是几万字的整篇正文，塞进 Prompt 后模型直接"忘掉问题"。

**修复**

| 配置项 | 值 | 含义 |
| --- | --- | --- |
| `VECTOR__CHUNK_TOP_K` | 30 | 初召回宽召回，交给精排/校验收敛 |
| `VECTOR__RERANKED_TOP_K` | 8 | 最终进入 Prompt 的最大条数 |
| `VECTOR__MIN_SCORE` | 0.35 | 精排/相似度下限 |
| `VECTOR__MAX_CONTEXT_CHARS` | 3000 | 单条引用最大字符数 |
| `VECTOR__MAX_PROMPT_CONTEXT_CHARS` | 12000 | 参考资料总预算，超了停止追加 |

同时检索层对"父分段过长"的情况改为**改用命中的子分段原文**，不再把整篇父段塞进去。

---

### 问题 5：精排请求体格式不兼容

**之前的现象**

本地精排服务一接就 `400`，只能静默降级成 RRF。

**根因**

不同实现的 `/rerank` 请求体不一样：

| 实现 | 请求体 |
| --- | --- |
| DashScope（百炼） | `{"input": {"query": ..., "documents": [...]}, "parameters": {...}}` |
| llama.cpp / vLLM / Jina | `{"query": ..., "documents": [...], "top_n": ...}`（顶层） |
| TEI | `{"query": ..., "texts": [...]}` |

旧代码一律发 DashScope 格式，本地服务当然不认。

**修复**（[rerank.py](file:///d:/code/rag/know-engine-python/app/services/rerank.py)）

- `build_rerank_body()`：按端点地址分支构造；
- `parse_rerank()`：统一解析 `output.results` / `results` / 直接数组三种响应；
- `400/422` 时自动把 `documents` 换成 `texts` 重试一次（TEI 兼容）；
- 响应解析不出分数视为失败，走 RRF 降级。

---

### 问题 6：向量维度错配 →「检索暂时不可用」

**之前的现象**

聊天页问「Redis 的持久化方案怎么选?」，回复头部提示：

> 检索暂时不可用，已按通用知识回答

后端日志：

```
BadRequestError: The query vector has a different number of dimensions [768]
than the document vectors [1536]
```

**根因**

- 向量模型换成了 `embeddinggemma-2:440m`（输出 **768 维**）；
- ES 索引 `know-engine-chunks-v1` 的 `dense_vector` 还是按旧模型建的 **1536 维**；
- 维度必须完全一致，否则 KNN 直接 400，检索整条链路失败。

**修复**（三处兜底）

1. [embedding.py](file:///d:/code/rag/know-engine-python/app/services/embedding.py) 新增 `probe_dim()`：真实调用一次 `/embeddings`，按返回长度确定维度（换模型自动重探）；
2. [vector_store.py](file:///d:/code/rag/know-engine-python/app/infra/vector_store.py) 的 `ensure_index()`：建索引前校验 mapping 维度——空索引自动删了重建，非空索引只报错不擅自删数据；
3. `search()` 的 KNN 路加兜底：维度错配时降级为全文检索，而不是整个检索失败。

**现在的效果**

```
embedding_dim_detected  dim=768  model=embeddinggemma-2:440m  was=1536
es_index_created        dim=768
```

聊天页不再出现「检索暂时不可用」。

---

### 问题 7：精排 500 —— 长文档把上下文撑爆

**之前的现象**

精排接口用短文本测试永远是好的，一接真实检索就 500，然后静默降级 RRF：

```
rerank_failed_fallback_rrf  docs=30  error="Server error '500 Internal Server Error' for url 'http://127.0.0.1:8080/rerank'"
```

**根因**

llama.cpp 默认 `--ubatch-size 512`。实测边界：

| 单条文档长度 | 结果 |
| --- | --- |
| 300 字 | ✅ OK（185 tokens） |
| 500 字 | ✅ OK（303 tokens） |
| **700 字** | ❌ 500 |
| 1000 字 | ❌ 500 |

真实分段普遍 800~1500 字，所以每次必挂。

**修复**

1. 服务端启动参数：`-c 8192 -b 4096 --ubatch-size 4096`（脚本已更新）；
2. 代码侧兜底：送精排前截断——单条最多 **1000 字**、最多 **20 条**（见 `rerank.py` 的 `_MAX_DOC_CHARS` / `_MAX_DOCS`）。

**现在的效果**

```
20 条 × 1000 字   →  ✅ 成功，12100 tokens，CPU 耗时约 7 秒
```

精排真正生效，不再默默降级。

---

### 问题 8：模型用途散落在环境变量里

**之前的现象**

对话、图片理解、语音、向量、精排、文生图各自从环境变量读配置；换模型要改 `.env` 再重启，而且同一个模型文件可能被不同用途误用。

**修复**

- 新增 `model_role_binding` 表（`role` → `model_id`）；
- `ROLE_SPECS` 定义六个用途：`chat` / `vision` / `speech` / `embedding` / `rerank` / `image_gen`；
- 解析顺序：**显式绑定 → 同用途默认模型 → 环境变量兜底**；
- 前端「模型配置」页新增「模型用途」区块，每个用途一个下拉，留空即回退；
- 连通性测试按用途分别校验（精排会真的发一次 `/rerank` 并解析结果）。

---

## 九、实测结果汇总

| 提问 | 修复前 | 修复后 |
| --- | --- | --- |
| Redis 的持久化方案怎么选 | 「检索暂时不可用」/ 精排 500 降级 | **3 条**，头条即「Redis的RDB和AOF区别」 |
| MySQL 索引失效的场景有哪些 | 混杂无关段落 | **1 条**，正是「索引失效的情况」 |
| 倒车入库的操作流程是什么 | **8 条**无关引用 | **0 条**，明确提示无相关资料 |

数据侧：151 个分段（150 条向量 + 1 条父段）已用 768 维模型重新向量化，MySQL 与 ES 一致。

---

## 十、日常运维

### 开机 / 重启后

精排服务不会自启，需要手动拉起：

```powershell
& D:\models\rerank\start-rerank.ps1
```

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health   # status: ok
```

### 启动后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8009 --reload
```

> 如果提示 `[Errno 10048] 端口已被占用`，说明已有实例在跑（可能是带 `--reload` 的旧进程），
> 先确认 `Get-NetTCPConnection -LocalPort 8009 -State Listen` 并结束对应进程。

### 换向量模型后必须做两件事

1. **重建 ES 索引**：新模型维度不同时，`ensure_index()` 对空索引会自动重建；索引非空会拒绝并报 `es_index_dim_mismatch`；
2. **重跑历史文档的向量化**：语义空间变了，旧向量与新向量不可比。

### 调阈值

`VECTOR__MIN_SCORE=0.35` 是配合 llama.cpp 原始 logits 的口径。换精排模型后：

- 如果返回 0~1 概率（多数云端 API），阈值 0.3~0.5 合理；
- 如果返回原始 logits（llama.cpp），0.35 约等于 sigmoid 后 0.59，偏严格，可按实际效果降到 0.0 ~ 0.2。

### 关键日志

| 日志 | 含义 |
| --- | --- |
| `rerank_done pairs=.. kept=.. dropped=..` | 精排成功（正常） |
| `rerank_failed_fallback_rrf` | 精排失败降级（需排查） |
| `rerank_skipped` | 未配置密钥被跳过 |
| `embedding_dim_detected` | 探测到向量维度变化 |
| `es_index_dim_mismatch` | 索引维度与模型不一致（索引非空，需人工处理） |
| `retrieval_no_relevant` | 门控过滤后无相关结果（正常行为） |

---

## 十一、代码位置速查

| 能力 | 文件 |
| --- | --- |
| 精排服务与请求体适配 | [rerank.py](file:///d:/code/rag/know-engine-python/app/services/rerank.py) |
| 向量模型与维度探测 | [embedding.py](file:///d:/code/rag/know-engine-python/app/services/embedding.py) |
| ES 索引 / KNN / 词面门控 | [vector_store.py](file:///d:/code/rag/know-engine-python/app/infra/vector_store.py) |
| 召回 + 聚合 + 引用 | [retrieval.py](file:///d:/code/rag/know-engine-python/app/modules/rag/retrieval.py) |
| 模型用途与绑定 | [service.py](file:///d:/code/rag/know-engine-python/app/modules/model/service.py) |
| 切片（标题识别 / 父子分段） | [splitters.py](file:///d:/code/rag/know-engine-python/app/modules/document/splitters.py) |
| 检索配置项 | [settings.py](file:///d:/code/rag/know-engine-python/app/config/settings.py) |
| 前端模型配置页 | [ModelConfigView.vue](file:///d:/code/rag/know-engine-python/web/src/features/admin/ModelConfigView.vue) |
| 精排启动脚本 | `D:\models\rerank\start-rerank.ps1` |
</content>
</article>
</Write>

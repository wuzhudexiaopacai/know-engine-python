# 本地模型容器化部署（Docker 内跑一套本地模型）

> 适用场景：原本把对话 / 向量 / 精排 / 语音模型装在宿主机（Ollama、llama.cpp、FunASR），
> 业务已经容器化，导致「容器里的 localhost 不是宿主机的 localhost」、换机器要重装一遍环境。
> 本文把这几个模型也收进 Compose，形成一个自洽的本地栈。
>
> 一句话：**业务容器不再依赖宿主机环境，模型与业务在同一个 Docker 网络里互相用服务名访问。**

---

## 1. 为什么不能直接沿用宿主机模型

| 现象 | 原因 |
| --- | --- |
| 业务容器报 `Connection refused` 调 `http://127.0.0.1:11434` | 容器内的 `127.0.0.1` 指容器自身，宿主机端口不在这个命名空间里 |
| 换一台机器就要重装 Ollama / llama.cpp / FunASR | 模型运行时没有随代码交付，环境是「手工装出来的」 |
| 精排、语音模型各自一个 PowerShell 窗口 | 没有进程守护，关窗即停，无法和业务一起起停 |
| 多副本部署时模型地址各不相同 | 服务发现靠人工填 IP，无法用服务名解析 |

项目里已有 `_rewrite_local_url()` 兜底：容器内会把 `localhost / 127.0.0.1` 自动改写成
`host.docker.internal`（见 [service.py](file:///d:/code/rag/know-engine-python/app/modules/model/service.py#L84-L102)），
但那要求模型仍跑在宿主机、且宿主机端口可达。本文给的是**彻底容器化**的方案。

---

## 2. 组件清单

| 组件 | 镜像 | profile | 端口 | 能力 | 接口 | 是否必需 |
| --- | --- | --- | --- | --- | --- | --- |
| `ollama` | `ollama/ollama:latest` | `models` | 11434 | 对话 / 视觉 / 记忆摘要 / 向量化 | OpenAI 兼容 `/v1/chat/completions`、`/v1/embeddings` | 必需（用云端模型时可不用） |
| `rerank` | `ghcr.io/ggml-org/llama.cpp:server` | `rerank` | 8080 | 检索精排（Cross-Encoder） | `POST /rerank` | 建议（缺省降级 RRF） |
| `speech` | 本地构建 `know-engine-funasr` | `speech` | 8001 | 语音转写（SenseVoice-Small） | `POST /v1/audio/transcriptions` | 可选 |
| `ollama-init` | `ollama/ollama:latest` | `init` | — | 一次性拉模型 | — | 首次需要 |

> profile 是 `docker compose` 的「按需启用」开关：只有显式启用（`--profile xxx` 或
> `.env` 里的 `COMPOSE_PROFILES=models,rerank`）才会启动该服务。这正是下面两种模式的实现方式。

对应编排文件：

| 文件 | 作用 |
| --- | --- |
| [docker-compose.local-models.yml](file:///d:/code/rag/know-engine-python/docker-compose.local-models.yml) | 三个模型服务 + 把 `api` 的默认模型地址切到本地 |
| [deploy/local-models/funasr.Dockerfile](file:///d:/code/rag/know-engine-python/deploy/local-models/funasr.Dockerfile) | FunASR 语音服务镜像（CPU 版 PyTorch） |

> **为什么精排不用 Ollama**：Ollama 只有 `/api/chat`、`/api/embed`，没有 `/rerank`，
> 也没有加载 Cross-Encoder 分类头的能力。把 reranker 放进 Ollama 只会退化成向量模型。

---

## 3. 目录、端口与数据卷

```text
know-engine-python/
├── docker-compose.local-models.yml
├── deploy/local-models/funasr.Dockerfile
└── models/                        ← 新建，已加入 .gitignore
    ├── rerank/
    │   └── bge-reranker-v2-m3-Q8_0.gguf   ← 需手动下载（约 606MB）
    └── ollama/                    ← 可选：离线 GGUF 放这里，用 Modelfile 导入
```

| 数据卷 | 挂载点 | 内容 |
| --- | --- | --- |
| `ollama-data`（命名卷） | `/root/.ollama` | `ollama pull` 下载的模型权重、配置 |
| `speech-models`（命名卷） | `/models` | ModelScope 缓存的 SenseVoice 模型 |
| `./models/rerank`（绑定挂载） | `/models`（只读） | GGUF 精排模型文件 |
| `./models/ollama`（绑定挂载） | `/models`（只读） | 离线 GGUF（可选） |

端口分工（与宿主已有服务冲突时改左侧映射即可）：

| 宿主端口 | 容器 | 用途 |
| --- | --- | --- |
| 11434 | ollama | OpenAI 兼容接口 |
| 8080 | rerank | 精排 |
| 8001 | speech | 语音转写 |
| 8009 | api | 业务（原有） |

---

## 4. 前置条件

- Docker Engine 24+ / Docker Desktop 4.30+，Compose v2（`docker compose version` 可查）。
- 磁盘：对话 7B 约 5GB、向量模型约 1.2GB、精排 0.6GB、语音 1GB（含镜像与依赖），建议预留 **20GB**。
- 内存：仅 1.5B 对话模型约 6GB；**7B（Q4）建议 16GB 以上**；同时驻留对话 + 向量建议 20GB。
- GPU（可选）：NVIDIA 显卡需装 `nvidia-container-toolkit`，并在 compose 里取消 GPU 段注释。
- 网络：首次需要能访问 Docker Hub / GHCR / ModelScope；离线部署见第 8 节。

---

## 5. 快速开始

### 5.1 准备模型目录

```powershell
New-Item -ItemType Directory -Force models\rerank, models\ollama | Out-Null
```

下载精排模型（二选一）：

```powershell
# 方式 A：ModelScope 命令行（推荐，国内快；在已激活 .venv 的终端执行）
.\.venv\Scripts\python.exe -m pip install modelscope
modelscope download --model gpustack/bge-reranker-v2-m3-GGUF `
  bge-reranker-v2-m3-Q8_0.gguf --local_dir models\rerank

# 方式 B：浏览器打开 https://modelscope.cn/models/gpustack/bge-reranker-v2-m3-GGUF
#         下载 bge-reranker-v2-m3-Q8_0.gguf，放到 models\rerank\
```

确认文件存在：

```powershell
Get-ChildItem models\rerank        # 应看到 bge-reranker-v2-m3-Q8_0.gguf
```

> 文件名不同时，用 `RERANK_MODEL_FILE=你的文件名.gguf` 覆盖，或在 `.env` 里设置。

### 5.2 两种启动模式（按场景选一个）

编排文件用 profile 区分「模型是否也进容器」，`MODEL_HOST` / `RERANK_HOST` / `SPEECH_HOST`
决定业务访问各模型服务的主机名：

| | 模式 A：全本地（模型也在容器） | 模式 B：仅业务容器（模型在宿主机） |
| --- | --- | --- |
| 适用 | 交付 / 部署 / 换机器即用 | 本机已有 Ollama 等服务，想容器化业务 |
| 启用 profile | `models`（+ `rerank` / `speech`） | 不启用 |
| 主机变量 | 默认值即可（`ollama` / `rerank` / `speech`） | `MODEL_HOST` / `RERANK_HOST` / `SPEECH_HOST` 都设 `host.docker.internal` |
| 模型地址 | `http://ollama:11434/v1` | `http://host.docker.internal:11434/v1` |
| 前置 | 先拉镜像、拉模型、放 GGUF | 宿主机模型须监听 `0.0.0.0` |
| 容器 | ollama / rerank / speech + api | 只有 api（和中间件） |

**模式 A：业务 + 模型一起起**

```powershell
# 方式 1：命令行指定 profile
docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml `
  --profile models --profile rerank up -d --build

# 方式 2：写进 .env，之后命令不用带 profile
# COMPOSE_PROFILES=models,rerank
docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml up -d --build
```

**模式 B：只起业务，模型还在宿主机**

```powershell
# .env 增加三行（缺一不可：Ollama / 精排 / 语音分别在宿主机不同端口）
# MODEL_HOST=host.docker.internal
# RERANK_HOST=host.docker.internal
# SPEECH_HOST=host.docker.internal
docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml up -d --build
```

此时容器内访问 `host.docker.internal:11434 / 8080 / 8001`，即宿主机的 `localhost` 同名端口；
宿主机上的模型必须监听 `0.0.0.0`（Ollama 设 `OLLAMA_HOST=0.0.0.0:11434`），只听 `127.0.0.1` 会连不上。

> **开发环境（后端直接 `uvicorn` 跑在宿主机）**：既不用模式 A 也不用 B —— 在「模型配置」里填
> `http://localhost:11434/v1` 即可，系统会把它识别为本地端点（免 API Key），无需任何 compose 参数。

启动后核对：

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml ps
docker exec ke-api printenv | Select-String "LLM__BASE_URL|RERANK__BASE_URL|SPEECH__BASE_URL"
```

预期（模式 A）：`LLM__BASE_URL=http://ollama:11434/v1`；
模式 B：`LLM__BASE_URL=http://host.docker.internal:11434/v1`。

### 5.3 拉取对话与向量模型

**方式 A：一次性任务（推荐，会自动等 ollama 就绪）**

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml `
  --profile models --profile init run --rm ollama-init
```

> `ollama-init` 依赖 `ollama` 服务，所以必须同时启用 `models` profile。

**方式 B：手动进入容器**

```powershell
docker exec -it ke-ollama ollama pull qwen2.5:7b
docker exec -it ke-ollama ollama pull qwen2.5:1.5b     # 记忆摘要等轻任务
docker exec -it ke-ollama ollama pull bge-m3           # 向量化
docker exec ke-ollama ollama list                      # 确认已下载
```

> 模型名可通过 `.env` 调整：`LOCAL_CHAT_MODEL` / `LOCAL_FLASH_MODEL` / `LOCAL_EMBEDDING_MODEL`。
> `ollama-init` 只拉对话与向量两个；轻量摘要模型按需手动补。

### 5.4 验证三个服务

```powershell
# 对话（OpenAI 兼容）
curl http://localhost:11434/v1/models

# 精排
curl -X POST http://localhost:8080/rerank -H "Content-Type: application/json" `
  -d '{"model":"bge-reranker-v2-m3","query":"Redis 持久化","documents":["RDB 快照","AOF 日志"],"top_n":2}'

# 语音（先按 5.5 启动 speech）
curl http://localhost:8001/v1/models
```

精排正常返回形如：

```json
{"results":[{"index":0,"relevance_score":0.2245},{"index":1,"relevance_score":-4.3372}]}
```

> 注意：llama.cpp 返回的是**原始 logits**（可为负），不是 0~1 概率；
> 系统内部会先做 sigmoid 归一，再用「top1 下限 + 相对差距」双门控，无需手工换算。

### 5.5 启动语音服务（可选）

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml `
  --profile speech up -d --build speech
docker logs -f ke-speech        # 首次会从 ModelScope 下载约 936MB
```

看到 `Uvicorn running on http://0.0.0.0:8001` 即就绪；模型缓存在 `speech-models` 卷里，重建不重复下载。

---

## 6. 接入主应用（模型配置）

登录后台 → **模型配置 → 新增模型**，按用途分别录入。容器内部访问一律用**服务名**：

| 用途 | 服务提供商 | 模型名称 | 接口地址 | API Key |
| --- | --- | --- | --- | --- |
| 对话 `chat` | 本地部署 | `qwen2.5:7b` | `http://ollama:11434/v1` | 留空 / 任意 |
| 记忆摘要 `memory` | 本地部署 | `qwen2.5:1.5b` | `http://ollama:11434/v1` | 留空 / 任意 |
| 视觉 `vision`（可选） | 本地部署 | `qwen2.5vl:7b` | `http://ollama:11434/v1` | 留空 / 任意 |
| 向量化 `embedding` | 本地部署 | `bge-m3` | `http://ollama:11434/v1` | 留空 / 任意 |
| 检索精排 `rerank` | 本地部署 | `bge-reranker-v2-m3` | `http://rerank:8080` | 留空 |
| 语音识别 `speech` | 本地部署 | `sensevoice` | `http://speech:8001/v1` | 留空 |

> 视觉模型需要单独拉取（如 `docker exec ke-ollama ollama pull qwen2.5vl:7b`），
> 并在模型配置里勾选「支持视觉」；否则文档图片理解与对话看图都会跳过视觉能力。

然后在「模型用途」区块把每个用途绑定到对应模型（留空则回退到同用途默认模型 / 环境变量）。

> **接口地址要带 `/v1` 还是不带？**
> - Ollama 的 OpenAI 兼容接口在 `/v1` 下：`http://ollama:11434/v1`；
> - 精排是 llama.cpp 的 `/rerank`，只填根地址 `http://rerank:8080`（代码自动补路径）；
> - 语音是 OpenAI Whisper 协议，代码会请求 `{base}/audio/transcriptions`，FunASR 的兼容路径带 `v1`，
>   所以填 `http://speech:8001/v1`。

### 6.1 寻址规则（很重要）

系统对「本地端口」有两条既有规则，选对地址才能免 Key、少踩坑：

| 规则 | 行为 | 出处 |
| --- | --- | --- |
| 本地端点免 Key | `provider=local`，或主机名属于 `localhost / 127.0.0.1 / 0.0.0.0 / ::1 / host.docker.internal` 时，允许空 API Key | `_is_local_endpoint()` |
| 回环地址改写 | 容器内把 `localhost / 127.0.0.1` 改写成 `host.docker.internal`，宿主机直跑则反向改回 `localhost` | `_rewrite_local_url()` |

因此有两条可用路线：

| 路线 | 接口地址 | 适用 |
| --- | --- | --- |
| **A. 容器网络（推荐）** | `http://ollama:11434/v1` | 模型与业务同 compose；走内部网络，不经过宿主机端口 |
| **B. 宿主机端口** | `http://localhost:11434/v1` | 沿用旧的宿主机模型配置；容器内自动改写为 `host.docker.internal` |

> 走路线 A 时，**「服务提供商」必须选「本地部署」**，否则服务名不在本地主机名单里，空 Key 会被拒绝。
> 路线 B 依赖 `docker-compose.prod.yml` 里已配置的 `extra_hosts: host.docker.internal:host-gateway`。

### 6.2 切到本地向量模型后必须重建索引

`bge-m3` 输出 **1024 维**；如果之前用的是 768 / 1536 维模型，ES 索引维度不一致会导致 KNN 直接 400：

- 空索引：`ensure_index()` 会自动按新维度重建；
- 非空索引：只报 `es_index_dim_mismatch`，不擅自删数据 —— 需要手动重建索引并**重新向量化全部文档**。

日志里会出现 `embedding_dim_detected dim=1024 model=bge-m3 was=768`，属正常提示。

---

## 7. 资源与性能

### 7.1 模型体积与内存

| 模型 | 量化/体积 | 内存占用（约） | CPU 可用 | 说明 |
| --- | --- | --- | --- | --- |
| `qwen2.5:7b` | Q4 约 4.7GB | 6~8GB | ✅ 较慢 | 通用对话主力，16GB 内存起 |
| `qwen2.5:1.5b` | Q4 约 1GB | 2~3GB | ✅ 流畅 | 摘要 / 标题 / 推荐问题 |
| `bge-m3` | 约 1.2GB | 2GB | ✅ | 多语言向量，1024 维 |
| `bge-reranker-v2-m3` | Q8_0 约 606MB | 1.5GB | ✅ | 精排，20 条×1000 字约 7 秒（CPU） |
| SenseVoice-Small | 约 936MB | 1GB | ✅ | 10 秒音频 1~3 秒 |

### 7.2 GPU 加速（可选）

宿主机先装 `nvidia-container-toolkit`，然后取消 `docker-compose.local-models.yml` 里对应服务
`deploy.resources` 段的注释。Ollama 也支持在 `.env` 中按需换更大的模型（如 `qwen2.5:14b`）。

语音服务改 GPU：把 `--device cpu` 改成 `--device cuda`，并使用 CUDA 版 PyTorch 基础镜像
（默认 Dockerfile 装的是 CPU 版，体积小、无显卡也能跑）。

### 7.3 资源限制建议

```yaml
# 片段：给 ollama 限制上限，避免把宿主内存吃满
    deploy:
      resources:
        limits:
          memory: 10g
```

> 内存不足的典型表现：容器 `Exit Code 137`，`docker logs ke-ollama` 看到 `Killed`。

---

## 8. 离线 / 内网部署

### 8.1 镜像离线

```powershell
# 有网机器
docker pull ollama/ollama:latest
docker pull ghcr.io/ggml-org/llama.cpp:server
docker save ollama/ollama:latest ghcr.io/ggml-org/llama.cpp:server -o local-models-images.tar

# 内网机器
docker load -i local-models-images.tar
```

语音镜像在目标机构建最省事；若目标机无法联网构建，可按 5.5 在有网机器 `docker build` 后同样 `save/load`。

### 8.2 Ollama 模型离线

在**有网**机器拉好模型后，直接拷命名卷数据：

```powershell
docker run --rm -v know-engine-python_ollama-data:/data -v ${PWD}:/backup alpine `
  tar czf /backup/ollama-models.tar.gz -C /data .
# 内网：解压回同名卷后重启 ollama
```

或者用 GGUF + Modelfile 导入：

```text
# models/ollama/Modelfile
FROM /models/qwen2.5-7b-instruct-q4_K_M.gguf
PARAMETER num_ctx 8192
```

```powershell
docker exec ke-ollama ollama create qwen2.5:7b -f /models/Modelfile
```

### 8.3 精排模型离线

GGUF 本身就是单文件，直接把 `bge-reranker-v2-m3-Q8_0.gguf` 拷进 `models/rerank/` 即可，无额外依赖。

### 8.4 语音模型离线

先在有网环境启动一次 `speech`，让模型落到 `speech-models` 卷，再导出卷：

```powershell
docker run --rm -v know-engine-python_speech-models:/data -v ${PWD}:/backup alpine `
  tar czf /backup/speech-models.tar.gz -C /data .
```

---

## 9. 日常运维

| 操作 | 命令 |
| --- | --- |
| 查看状态（含模型） | `docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml --profile models --profile rerank ps` |
| 查看状态（仅业务） | `docker compose -f docker-compose.prod.yml -f docker-compose.local-models.yml ps` |
| 看某个模型日志 | `docker logs -f ke-ollama` / `ke-rerank` / `ke-speech` |
| 已加载模型 | `docker exec ke-ollama ollama ps` |
| 新增模型 | `docker exec -it ke-ollama ollama pull <model>` 后在后台绑定用途 |
| 删除模型 | `docker exec ke-ollama ollama rm <model>` |
| 重启精排 | `docker restart ke-rerank` |
| 只重启业务 | `docker restart ke-api`（模型容器常驻，不重复加载） |
| 更新镜像 | `docker compose ... --profile models pull ollama && docker compose ... --profile models --profile rerank up -d` |
| 切到模式 B | `.env` 把 `MODEL_HOST` / `RERANK_HOST` / `SPEECH_HOST` 改成 `host.docker.internal`，并清空 `COMPOSE_PROFILES`，再 `up -d` |
| 切回模式 A | 删掉上面三个 `*_HOST`（或改回 `ollama` / `rerank` / `speech`），`COMPOSE_PROFILES=models,rerank`，再 `up -d` |
| 清理全部数据 | `docker compose ... down -v`（**会删除 `ollama-data` / `speech-models` 卷**） |

> 与 `docker-compose.multi.yml` 叠加时，`api` 会被 nginx 负载均衡；模型服务仍按服务名访问，无需额外配置。

---

## 10. 排障速查

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| 模式 B 报 `Connection refused ...11434` | 宿主机 Ollama 只监听 `127.0.0.1`，或 `MODEL_HOST` 仍为 `ollama` | 宿主机设 `OLLAMA_HOST=0.0.0.0:11434` 并重启；`.env` 三个 `*_HOST` 都改成 `host.docker.internal` |
| 模式 A 里 `api` 连不上 `ollama` | profile 没启用，模型容器根本没起 | 加 `--profile models` 或 `.env` 写 `COMPOSE_PROFILES=models` |
| `api` 日志 `Connection refused ... :11434` | 地址填了 `127.0.0.1`，且未走 `host.docker.internal` 改写；或 ollama 未起 | 改用 `http://ollama:11434/v1`；`docker ps` 确认 `ke-ollama` 为 healthy |
| 保存模型时报「API Key 不能为空」 | 用服务名访问，但「服务提供商」没选「本地部署」 | 提供商改为「本地部署」，或改用 `host.docker.internal` 地址 |
| `ke-rerank` 反复重启 | `models/rerank` 里没有 GGUF，或文件名与 `RERANK_MODEL_FILE` 不一致 | `docker logs ke-rerank`；补齐文件或设置 `RERANK_MODEL_FILE` |
| 精排短文本正常、长文档 500 | `--ubatch-size` 太小 | 已默认 4096；不要删掉该参数 |
| 聊天提示「检索暂时不可用」 | 换向量模型后 ES 索引维度不一致 | 重建索引 + 重新向量化（见 6.2） |
| `ollama pull` 很慢 / 超时 | 网络问题 | 配置代理，或按第 8 节离线导入 |
| `ollama` 容器 `Exit 137` | 内存不足被 OOM kill | 换更小模型（1.5B），或调大宿主内存 / 减少 `OLLAMA_MAX_LOADED_MODELS` |
| `speech` 启动后端口无响应 | 首次在下载 936MB 模型 | `docker logs -f ke-speech` 等待 `Uvicorn running` |
| 语音 404 `/audio/transcriptions` | 地址没带 `/v1` | 填 `http://speech:8001/v1` |
| Windows 下 `models/rerank` 挂载为空 | 路径相对了错误的目录 | 必须在仓库根目录执行 compose，或用绝对路径 |
| 模型回答很慢 | CPU 推理 | 换 1.5B / 用 GPU / 降 `num_ctx`，或改用云端模型 |

---

## 11. 组件替代方案

| 需求 | 本项目默认 | 可替换 | 说明 |
| --- | --- | --- | --- |
| 对话 / 向量 | Ollama | vLLM（GPU，高吞吐）、Xinference | 只要提供 OpenAI 兼容 `/v1`，后台填地址即可 |
| 精排 | llama.cpp | TEI（`POST /rerank`）、vLLM | 精排协议已适配 TEI 的 `texts` 字段，400/422 会自动重试 |
| 语音 | FunASR SenseVoice | faster-whisper、Whisper 兼容端点 | 走 OpenAI `/audio/transcriptions` |

替换后只需在后台改「接口地址 / 模型名称」，无需改业务代码；
但**换向量模型必须重建索引**，**换精排模型必须重新校准门控阈值**
（详见 [local-rerank-deploy.md](file:///d:/code/rag/know-engine-python/docs/local-rerank-deploy.md)）。

---

## 12. 相关文件

| 文件 | 作用 |
| --- | --- |
| [docker-compose.local-models.yml](file:///d:/code/rag/know-engine-python/docker-compose.local-models.yml) | 本地模型编排 |
| [deploy/local-models/funasr.Dockerfile](file:///d:/code/rag/know-engine-python/deploy/local-models/funasr.Dockerfile) | 语音服务镜像 |
| [docker-compose.prod.yml](file:///d:/code/rag/know-engine-python/docker-compose.prod.yml) | 主编排（`extra_hosts` 在这里） |
| [app/modules/model/service.py](file:///d:/code/rag/know-engine-python/app/modules/model/service.py) | 模型用途解析、本地地址改写、免 Key 判定 |
| [app/services/rerank.py](file:///d:/code/rag/know-engine-python/app/services/rerank.py) | 精排请求体适配与降级 |
| [app/services/speech.py](file:///d:/code/rag/know-engine-python/app/services/speech.py) | 语音转写传输选择 |
| [docs/local-rerank-deploy.md](file:///d:/code/rag/know-engine-python/docs/local-rerank-deploy.md) | 精排原理、阈值校准、历史踩坑 |
| [docs/local-speech-deploy.md](file:///d:/code/rag/know-engine-python/docs/local-speech-deploy.md) | 语音模型宿主机部署与排障 |

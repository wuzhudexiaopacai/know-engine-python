# KnowEngine-Python · 企业级智能知识引擎

基于 **FastAPI + Vue3（TypeScript）** 的企业级 RAG 知识引擎：
文档接入 → 多源混合检索 → 意图识别 / 改写 / 路由 → 精排 → **SSE 流式生成（进度 / 引用溯源 / 结构化卡片）**
→ 多版本知识库治理 → 审核式问答缓存。中间件全部用 **Docker Compose** 部署，优先使用现成官方 / 可信镜像。

> 全链路「可选降级」：即便不启动任何中间件，后端也能以 SQLite + 进程内向量 / 缓存 + 本地哈希向量兜底跑通全部接口，
> 便于本地体验；生产环境切换到 MySQL / Redis / Elasticsearch / Neo4j / MinIO 后接口保持不变。

---

## 1. 技术栈

| 层次 | 选型 |
| --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.0（async）+ Pydantic v2 + pydantic-settings |
| 前端 | Vue 3 `<script setup>` + TypeScript（严格模式）+ Vite + Pinia + vue-router + Element Plus |
| 中间件 | MySQL 8 · Redis 7 · Elasticsearch 8 · Neo4j 5 · MinIO（`cgr.dev/chainguard/minio`） |
| 弹性 | tenacity 指数退避重试 · 三态熔断器 · 全链路降级兜底 · APScheduler 补偿任务 |
| 可观测 | structlog 结构化日志（JSON / Console 双模式） |

## 2. 目录结构

```
know-engine-python/
├── app/
│   ├── main.py               # FastAPI 入口：生命周期 + 路由 + CORS + 静态前端托管
│   ├── config/settings.py    # 配置中心（env 前缀映射，敏感项走 .env）
│   ├── core/                 # 数据库 / 日志 / 异常 / 依赖注入 / 统一响应 R
│   ├── infra/                # 雪花ID / 分布式锁 / 三级缓存 / 熔断 / 事件总线 / JSON 修复
│   ├── models/               # ORM 实体 + 常量枚举 + 建表/查询辅助 + 种子数据
│   ├── services/             # LLM / Embedding / Rerank（重试 + 降级）
│   ├── modules/
│   │   ├── auth/             # 双身份认证（用户手机号 / 管理员工号）
│   │   ├── document/         # 分片上传 / 类型检测 / 解析 / 切片 / 向量化流水线
│   │   ├── rag/              # 意图 / 改写 / 路由 / 混合检索 / Text2SQL / Text2Cypher / 聚合
│   │   ├── chat/             # 会话与消息 + SSE 流式接口
│   │   └── qa_cache/         # 审核式问答缓存：挖掘 / 审核 / 上线 / 命中 / 准出
│   └── jobs/                 # 定时补偿任务（文档流水线 / 上传会话清理 / 缓存维护）
├── web/                      # Vue3 + TS 前端（Feature 驱动）
│   └── src/{api,types,stores,router,utils,components,features,styles}
├── sql/init/                 # MySQL 首次启动自动建库建账号脚本
├── docker-compose.dev.yml    # Dev：只启中间件（前后端本机源码运行）
├── docker-compose.prod.yml   # Pro：中间件 + 一体化应用（前端构建产物由后端托管）
├── Dockerfile                # Pro 使用的多阶段镜像（Node 构建 + Python 运行时）
├── .env.example              # 配置样例（复制为 .env）
└── requirements.txt
```

## 3. 快速开始

### 前置要求
- Docker + Docker Compose
- Python 3.11+（开发验证基于 3.12）
- Node 20+ 与 pnpm（`corepack enable` 即可获取 pnpm）

### 3.1 启动中间件

```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps   # 观察各服务健康状态转为 healthy
```

镜像清单（全部为现成镜像，无自建）：

| 服务 | 镜像 | 端口 | 说明 |
| --- | --- | --- | --- |
| MySQL | `mysql:8.0` | 3307（映射容器 3306） | 业务库；`sql/init` 首次启动自动建库建账号 |
| Redis | `redis:7-alpine` | 6379 | L2 缓存 + 分布式锁 |
| Elasticsearch | `docker.elastic.co/elasticsearch/elasticsearch:8.19.21` | 9200 | KNN / 全文 / 混合检索 |
| Neo4j | `neo4j:5-community` | 7474 / 7687 | Text2Cypher 图检索（含 APOC） |
| MinIO | `cgr.dev/chainguard/minio` | 9000 / 9001 | 对象存储（按要求使用 chainguard 镜像，控制台 http://localhost:9001） |

> **不想启动中间件？** 跳过本步即可，后端会自动降级：MySQL→本地 SQLite、Redis→进程内 TTL、
> ES→MySQL 向量 + numpy 余弦 / LIKE 全文、MinIO→本地磁盘、Neo4j→提示走知识库检索。

### 3.2 启动后端

```bash
# 建议使用虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1           # Windows PowerShell（Linux/macOS 用 source .venv/bin/activate）

pip install -r requirements.txt

# 可选：注入 LLM / Embedding 能力（不配也能跑，走降级话术 + 本地哈希向量）
copy .env.example .env                 # 按需填写 LLM__API_KEY 等

python -m uvicorn app.main:app --host 127.0.0.1 --port 8009 --reload
```

- API 文档：http://127.0.0.1:8009/api/docs
- 存活探针：http://127.0.0.1:8009/api/health

**改完代码的快速自检（不启动服务、不连中间件也能跑）**

```bash
python -m compileall -q app                                  # 语法/编码检查，无输出即通过
python -c "import app.main as m; print('routes:', len(m.app.routes))"   # 导入检查 + 路由装载
```

> 国内网络装依赖慢时可换镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`；
> 前端 `pnpm config set registry https://registry.npmmirror.com`（阿里云源）。
> Pro 镜像构建已内置这两个源（见 `Dockerfile`），无需额外配置。

### 3.3 启动前端

两种模式，任选其一：

**A. 开发模式（Vite 热更新，`/api` 代理到后端 8009）**

```bash
cd web
pnpm install
pnpm dev            # http://localhost:5173
```

**B. 生产 / 一体化模式（构建产物由 FastAPI 同源托管，无需额外端口）**

```bash
cd web
pnpm install
pnpm build          # 产物输出到 web/dist，vue-tsc 先做类型检查再打包
```

然后直接访问后端根路径即可：**http://127.0.0.1:8009/**
（`app/main.py` 会挂载 `web/dist`，并对非 `/api` 路径做 SPA 兜底回 `index.html`。）

### 3.4 一键全栈（Pro 编排）

前后端与中间件全部容器化，前端构建产物由 FastAPI 同源托管，只暴露一个 Web 端口：

```bash
docker compose -f docker-compose.prod.yml up -d --build
# 打开 http://localhost:8009/
```

- 应用镜像由 `Dockerfile` 两阶段构建：Node 构建 `web/dist` → Python 运行时安装依赖并托管静态资源
- **服务间互访一律用容器名**（`mysql:3306` / `redis:6379` / `elasticsearch:9200` / `neo4j:7687` / `minio:9000`），
  配置集中在 compose 的 `environment`，代码与镜像里不写死域名
- **库内只记「路径」，不记「地址」**：文档 / 图片落库的是对象 key（如 `converted/2026/10/xxx.md`），
  对外统一走同源相对路由 `/api/files/{key}`，换域名、换端口、上反向代理都不需要迁移数据
- **宿主访问用映射端口**：Web `8009`、MySQL `3307`（容器内仍是 3306）、Redis `6379`、ES `9200`、MinIO `9000/9001`
- 可选环境变量（在启动 compose 的 shell 里设置即可透传）：`APP_SECRET_KEY`、`LLM_API_KEY`、`LLM_BASE_URL`

### 3.5 演示账号

| 身份 | 账号 | 密码 | 可用页面 |
| --- | --- | --- | --- |
| 用户 | `13800138000` | `123456` | 智能问答 |
| 管理员 | `TEST001` | `123456` | 智能问答 · 知识文档 · 问答审核 |

登录页均需输入图形验证码（点击图片可刷新，一次性有效，有效期 5 分钟）。

## 4. 配置说明（`.env`）

键名与 `app/config/settings.py` 一一对应（`段__字段` 双下划线分隔），默认值已对齐 docker-compose：

```ini
# LLM / Embedding（OpenAI 兼容协议，默认阿里云 DashScope）
LLM__API_KEY=sk-xxxx                 # 留空则走降级：跳过改写/路由 + 本地哈希向量
LLM__CHAT_MODEL=qwen-plus
LLM__EMBEDDING_MODEL=text-embedding-v4

# 中间件（本机端口冲突时才需覆盖）
DB__URL=mysql+aiomysql://know_engine:know_engine@localhost:3307/know_engine?charset=utf8mb4
DB__FALLBACK_SQLITE=true             # 生产设为 false
REDIS__URL=redis://localhost:6379/0
VECTOR__ES_URL=http://localhost:9200
NEO4J__URI=bolt://localhost:7687
STORAGE__ENDPOINT=localhost:9000

# 应用
APP__ENV=dev
APP__SECRET_KEY=please-change-me     # 令牌签名密钥，生产必须覆盖
QA_CACHE__ENABLED=true               # 审核式问答缓存开关
QA_CACHE__TTL_DAYS=90                # 审核上线后的有效天数；<=0 表示永久有效
QA_CACHE__REVIEWER_IDS=TEST001       # 逗号分隔的审核管理员账号（工号）
```

完整项见 `.env.example`。

## 5. 工程亮点

- **大文件分片上传**：整文件 MD5 秒传 + 分片断点续传（缺失分片跳过）+ finalize 合并入库触发流水线；前端 `spark-md5` 增量哈希（不依赖 `crypto.subtle`，兼容局域网 http）。
- **多策略切片**：Markdown 父子分段 / Excel 双模式 / 长度 / 正则 / 分隔符 / 智能切片，支持按新配置对指定版本重切片。
- **多源混合检索**：ES KNN + 全文 + 混合，融合 Text2SQL、Text2Cypher 结构化问答，产出对比表卡片；父子分段扩展召回。
- **RAG 管道**：意图识别 → 查询改写 → 路由 → 检索 → 聚合 → Rerank 精排 → SSE 流式生成，全过程带**阶段进度事件**。
- **引用溯源 & 会话记忆**：答案附相似片段引用（来源 / 相似度 / 精排分）；滑动窗口多轮记忆。
- **审核式问答缓存**：高频问答挖掘 → 人工审核（通过 / 驳回）→ 上线命中 → 负反馈自动准出，全生命周期治理。
- **弹性与一致性**：tenacity 指数退避 + 三态熔断 + 全链路降级；乐观锁 `lock_version` 版本热切换；逻辑删除软删；雪花 ID；分布式锁；事件驱动的文档处理流水线 + APScheduler 补偿兜底。
- **企业级前端**：Feature 驱动目录、TypeScript 严格类型、统一 `BaseResult` 拆包与 401 跳登录、Pinia 认证态与路由守卫（角色感知）、设计 token + Element Plus、loading / error / empty 三态与空态兜底、无障碍（语义标签 / aria / 键盘可达 / focus-visible / prefers-reduced-motion）。

## 6. 主要接口

| 分组 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | `GET /api/auth/captcha` · `POST /api/auth/login` · `POST /api/auth/admin-login` · `GET /api/auth/me` | 图形验证码 / 双身份登录 / 当前身份恢复 |
| 文档 | `POST /api/document/upload/init` · `/upload/chunk` · `GET /upload/status` · `POST /upload/finalize` | 分片上传编排 |
| 文档 | `GET /api/document/list` · `/{doc_id}` · `/{doc_id}/versions` | 列表 / 详情 / 版本 |
| 文档 | `POST /api/document/{doc_id}/switch-version` · `/delete-version` · `/retry` · `/resplit` · `DELETE /{doc_id}` | 版本治理 / 失败重试 / 重切片 |
| 对话 | `POST /api/chat/stream`（SSE） | 流式问答：progress / token / reference / card / answer_message / done / error |
| 对话 | `GET /api/chat/conversations` · `/messages` · `DELETE /conversations/{id}` · `POST /feedback` | 会话管理与点赞点踩 |
| 缓存 | `GET /api/qa-cache/entries` · `POST /entries/{id}/approve` · `/reject` · `/disable` | 审核式问答缓存 |

> 全部接口以 OpenAPI（`/api/docs`）为准；统一响应信封 `{ success, code, message, data }`，分页 `{ total, page_num, page_size, records }`。

## 7. 常见问题

- **登录 / 页面正常，但问答返回「AI 生成服务当前不可用」**：未配置 `LLM__API_KEY`。填入有效的 DashScope Key 即恢复正常生成；未配置时后端按设计走降级话术，不影响其余功能。
- **`pnpm build` 失败**：确认已 `pnpm install`；Node ≥ 20。构建脚本为 `vue-tsc --noEmit && vite build`，类型错误会先于打包暴露。
- **改了前端源码没生效**：一体化模式下需重新 `pnpm build` 后再刷新；日常迭代建议用 `pnpm dev`（热更新 + `/api` 代理）。
- **端口占用**：后端端口经 `APP__PORT` 调整，并同步前端 `web/.env.development` 的 `VITE_API_TARGET`。

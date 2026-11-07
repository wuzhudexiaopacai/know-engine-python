# 统一日志服务（多实例）

> 目标：多机 / 多副本部署时，日志**统一汇入一处**，既能留档（文件），也能按请求链路检索（ES + DB），
> 不再需要逐台 `docker logs` 排查；同时不新增日志专用容器。

## 1. 总体结构

```text
                    ┌───────────────┐
   browser ────────▶│     Nginx     │
                    └───────┬───────┘
                ┌───────────┼───────────┐
                ▼           ▼           ▼
            ┌───────┐   ┌───────┐   ┌───────┐
            │ api-1 │   │ api-2 │   │ api-3 │   每个实例：
            └──┬─┬──┘   └──┬─┬──┘   └──┬─┬──┘   · stdout（docker logs）
               │ │         │ │         │ │      · logs/know-engine.log（每日滚动）
               │ └─────────┼─┼─────────┘ │      · ES 批量写入（有界队列，不阻塞）
               ▼           ▼ ▼           ▼
        ┌────────────┐  ┌──────────────────┐
        │   MySQL    │  │  Elasticsearch   │
        │operation_  │  │ know-engine-logs-*│
        │   log      │  │  （全实例共用）    │
        └────────────┘  └──────────────────┘
                 ▲              ▲
                 └──────┬───────┘
                        │
              Web「日志中心」按权限 log:view 检索
```

两条互补的日志通道：

| 通道 | 存储 | 回答的问题 | 写入方式 |
| --- | --- | --- | --- |
| 操作日志 | MySQL `operation_log` | 谁、什么时间、对什么、做了什么、结果如何 | 应用内异步队列批量落库 |
| 系统日志 | ES `know-engine-logs-YYYY.MM` | 请求链路、异常堆栈、耗时、第三方调用 | 日志 Handler → 有界队列 → 后台线程 bulk |
| 留档日志 | 本地 `logs/know-engine.log` | 容器/ES 都不可用时的兜底 | `TimedRotatingFileHandler` 每日滚动 |

## 2. 关键约定

1. **统一入口**：业务代码只用 `app.core.logging.get_logger()`，禁止 `print` / 裸 `logging.getLogger()`。
2. **统一格式**：所有日志（含 uvicorn / SQLAlchemy 等三方库）都经同一处理链，输出 JSON Lines。
3. **统一字段**：每条日志自动携带 `service / env / host / pid / instance`，能看出是哪个实例产生。
4. **统一链路**：`RequestContextMiddleware` 为每个请求生成或透传 `X-Request-ID`，
   并绑定 `request_id / method / path / client_ip`；鉴权依赖再绑定 `tenant_id / account_id`。
5. **统一脱敏**：`password / token / authorization / api_key / secret / cookie` 等字段递归打码，
   字符串里的 `Bearer xxx` 也会被打码，避免密钥进入文件与 ES。
6. **不阻塞业务**：ES 写入队列满、ES 超时、文件目录不可写，全部静默降级，只影响可观测性，不影响接口。

## 3. 日志字段

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `@timestamp` | 日志处理器 | ISO8601，带本地时区 |
| `level` | structlog | `INFO` / `WARNING` / `ERROR` 等 |
| `event` | 业务代码 | 事件名，如 `access`、`account_login` |
| `logger` | 业务代码 | 命名 logger，如 `app.access` |
| `service` / `env` | 配置 | 应用名与环境 |
| `host` / `pid` / `instance` | 运行时 | 主机名、进程号、实例标识（容器名/Pod 名） |
| `request_id` | 中间件 | 请求链路 ID，响应头 `X-Request-ID` 回写 |
| `tenant_id` / `account_id` | 鉴权依赖 | 多租户定位；平台超管可为空 |
| `method` / `path` / `status_code` / `duration_ms` | 中间件 | access 日志与系统日志通用 |
| `client_ip` | 中间件 | 优先取 `X-Forwarded-For` 第一跳 |

`operation_log`（操作日志）主要字段：

| 字段 | 说明 |
| --- | --- |
| `module` / `action` | 模块与动作，如 `document / delete`、`auth / login` |
| `target_type` / `target_id` | 操作对象（租户、账号、文档等） |
| `status` / `status_code` | `SUCCESS` / `FAILED` 与 HTTP 状态 |
| `request_id` | 与 ES 系统日志串联的关联键 |
| `ip` / `user_agent` / `duration_ms` | 请求上下文 |
| `detail` | 语义补充（JSON），如角色变更前后的角色码 |

## 4. 配置项

环境变量统一前缀 `LOGGING__`（沿用 `段__字段` 规则）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOGGING__LEVEL` | `INFO` | 日志级别 |
| `LOGGING__FILE_ENABLED` | `true` | 是否写本地文件 |
| `LOGGING__FILE_DIR` | `./logs` | 日志目录；容器建议挂载到共享卷 |
| `LOGGING__FILE_NAME` | `know-engine.log` | 文件名；支持 `{host}` / `{pid}` 占位（多副本共享卷时避免同时滚动同一个文件） |
| `LOGGING__RETENTION_DAYS` | `30` | 本地文件保留天数 |
| `LOGGING__ES_ENABLED` | `true` | 是否写 ES 集中索引 |
| `LOGGING__ES_INDEX_PREFIX` | `know-engine-logs` | 索引前缀，实际索引按月：`前缀-YYYY.MM` |
| `LOGGING__ES_BULK_SIZE` | `200` | 单批写入条数 |
| `LOGGING__ES_FLUSH_INTERVAL` | `2.0` | 队列最长等待秒数 |
| `LOGGING__ES_QUEUE_SIZE` | `20000` | 内存队列上限，满则丢弃 |
| `LOGGING__QUERY_MAX_SIZE` | `500` | 日志中心单页查询上限 |

ES 连接复用 `VECTOR__ES_URL / VECTOR__ES_USERNAME / VECTOR__ES_PASSWORD`，**无需新增日志容器**。

## 5. ES 索引与保留

- 索引按月：`know-engine-logs-2026.11`；同一集群内所有实例写同一个索引。
- 首次写入自动创建索引与字段映射（`@timestamp` 为 date，`level/request_id/tenant_id` 等为 keyword/long）。
- 保留策略建议用 ILM：日志索引 30~90 天滚动删除（按企业合规要求调整）；
  本地文件与 ES 保留期应保持一致，避免排障时「文件里有、ES 里没有」。
- 多实例时钟必须 NTP 同步，否则跨实例按时间排序会出现偏差。

## 6. 日志中心（Web）

路径 `/logs`，权限点 `log:view`（内置角色 `tenant_admin` 默认具备，平台超管自动具备）。

- **操作日志 Tab**：查 MySQL `operation_log`，支持关键字（账号/动作/路径/request_id）、模块、结果、时间范围。
- **系统日志 Tab**：查 ES，支持关键字、级别、模块、request_id、时间范围；
  ES 未配置或不可用时返回 `degraded=true`，页面展示降级提示而非报错。
- 租户账号只能看到本租户日志；平台超管可跨租户查询。

对应接口：

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/admin/logs/operations` | 操作日志分页查询 | `log:view` |
| GET | `/api/admin/logs/operations/modules` | 模块筛选下拉 | `log:view` |
| GET | `/api/admin/logs/system` | 系统日志（ES）分页查询 | `log:view` |

## 7. 排障手册

| 现象 | 排查顺序 |
| --- | --- |
| 页面报错 | 用响应头 `X-Request-ID` → 日志中心「系统日志」按 request_id 过滤 → 看该请求完整链路 |
| 某实例行为异常 | 系统日志按 `instance` 过滤，对比多个副本的同事件耗时与状态码 |
| 操作争议 | 操作日志按账号/模块/时间过滤，`detail` 里有语义字段；再用 `request_id` 关联系统日志 |
| ES 无数据 | 看启动日志 `log_es_*`；确认 `VECTOR__ES_URL`、索引是否创建、`LOGGING__ES_ENABLED` |
| 本地文件没有 | 容器内目录不可写（只读根文件系统）→ 挂载卷或关闭 `LOGGING__FILE_ENABLED` |

## 8. 多机部署检查清单

- [ ] 所有实例 `VECTOR__ES_URL` 指向同一 ES 集群。
- [ ] `LOGGING__ES_ENABLED=true`，且 ES 账号具备目标索引 `create_index` / `index` / `read` 权限。
- [ ] `LOGGING__FILE_DIR` 挂载到持久卷（或明确接受容器重建后本地文件丢失）；
      多副本共享同一卷时，`LOGGING__FILE_NAME` 使用 `{host}` 占位，确保每个实例写自己的文件。
- [ ] Nginx/网关透传 `X-Request-ID`（未透传时应用会自动生成，不影响链路）。
- [ ] 各实例主机名（容器名/Pod 名）唯一，`instance` 才能区分来源。
- [ ] ES 配置 ILM 保留策略，避免日志索引无限增长。

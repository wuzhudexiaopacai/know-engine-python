# KnowEngine-Python 一体化镜像：前端构建产物由 FastAPI 同源托管
#
# 阶段一：Node 构建 web/dist（vue-tsc 类型检查 + vite build）
# 阶段二：Python 运行时，安装依赖并拷入 app 与 dist
#
# 构建：docker build -t know-engine-api:latest .
# 运行：docker compose -f docker-compose.prod.yml up -d --build

# ---------- 阶段一：前端构建 ----------
FROM node:20-alpine AS web-build
WORKDIR /build
RUN corepack enable && corepack prepare pnpm@9 --activate
# npm 依赖走阿里云镜像（国内构建加速）
RUN pnpm config set registry https://registry.npmmirror.com
# 先拷依赖清单，利用镜像层缓存
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---------- 阶段二：Python 运行时 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 依赖单独一层，改代码不会触发重新安装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sql ./sql
COPY --from=web-build /build/dist ./web/dist

EXPOSE 8009

# 端口由 APP__PORT 控制（默认 8009），便于只改 compose 环境变量而不重建镜像
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${APP__HOST:-0.0.0.0} --port ${APP__PORT:-8009}"]

# FunASR + SenseVoice-Small 语音识别服务（CPU）
#
# 构建：docker build -f deploy/local-models/funasr.Dockerfile -t know-engine-funasr:latest .
# 说明：模型在首次启动时从 ModelScope 下载（约 936MB），缓存到 MODELSCOPE_CACHE。
#       内网离线部署请先在有网环境跑一次，再把 /models 目录拷进卷。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    MODELSCOPE_CACHE=/models/modelscope

# ffmpeg：mp3 / m4a / webm 等格式解码；缺了只能处理 wav
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# CPU 版 PyTorch：默认源会装 CUDA 版（数 GB），这里显式指定 CPU 索引
RUN pip install --no-cache-dir torch torchaudio \
      --index-url https://download.pytorch.org/whl/cpu

# funasr 提供 funasr.bin.server；modelscope 负责拉模型
RUN pip install --no-cache-dir \
      funasr modelscope fastapi uvicorn python-multipart

EXPOSE 8001

# 默认 0.0.0.0 监听；接口：POST /v1/audio/transcriptions（OpenAI Whisper 兼容）
CMD ["python", "-m", "funasr.bin.server", \
     "--model", "sensevoice", "--device", "cpu", "--port", "8001"]

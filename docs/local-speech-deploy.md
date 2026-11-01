# 本地语音识别部署指南（SenseVoice-Small + FunASR）

> 本文档从零开始，详细记录如何在本地（Windows + CPU）部署 **SenseVoice-Small** 语音识别模型，
> 并接入「知识引擎」系统的语音输入功能。全流程约 10 分钟（不含模型下载时间）。

---

## 1. 为什么选 SenseVoice-Small

语音识别（ASR）主流方案对比：

| 方案 | 中文效果 | 体积 | CPU 可跑 | 接口协议 |
|---|---|---|---|---|
| **SenseVoice-Small**（阿里 FunASR） | ⭐⭐⭐⭐⭐ | 270M 参数 / 936MB | ✅ | OpenAI Whisper 兼容 |
| Whisper-large-v3（OpenAI） | ⭐⭐⭐⭐ | 3GB | ✅（较慢） | OpenAI Whisper 兼容 |
| Paraformer-large（阿里） | ⭐⭐⭐⭐⭐ | 220M | ✅ | OpenAI Whisper 兼容 |
| Vosk | ⭐⭐⭐ | <100MB | ✅ | 专有 SDK |

**选 SenseVoice-Small 的理由：**
- 中文识别效果在开源模型里属第一梯队，中英混合场景表现好
- 体积小（936MB），纯 CPU 也能跑出可用速度（10 秒音频约 1-3 秒出结果）
- FunASR 官方提供 `funasr-server`，一键启动，接口是 OpenAI Whisper 兼容的 `/v1/audio/transcriptions`，和系统对接简单
- 支持多语言（中/英/日/韩/粤），含情感识别、标点恢复

---

## 2. 前置条件

- 操作系统：Windows 10/11
- Python：3.10 ~ 3.12（本项目用 3.12）
- 虚拟环境：`d:\code\rag\know-engine-python\.venv`
- 磁盘空间：≥ 2GB（模型 936MB + 依赖约 1GB）
- 内存：≥ 4GB（模型加载约占 1GB）

---

## 3. 安装依赖

SenseVoice-Small 依赖 **PyTorch** + **FunASR** + **ModelScope**。

### 3.1 安装 CPU 版 PyTorch

FunASR 需要 PyTorch。CPU 版体积小、安装快，不需要 NVIDIA 显卡。

```powershell
cd d:\code\rag\know-engine-python
.\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```

> ⚠️ **不要用默认的 `pip install torch`**，那样会装 CUDA 版（几 GB），本机没显卡用不了。
> `--index-url https://download.pytorch.org/whl/cpu` 指定 CPU 版索引。

验证：

```powershell
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__)"
# 预期输出：2.x.x+cpu
```

### 3.2 安装 FunASR + ModelScope

```powershell
.\.venv\Scripts\python.exe -m pip install funasr modelscope fastapi uvicorn python-multipart
```

- `funasr`：语音识别框架，含 `funasr-server` 命令
- `modelscope`：从 ModelScope（阿里魔搭社区）下载模型
- `fastapi` / `uvicorn` / `python-multipart`：funasr-server 依赖的 Web 框架

验证：

```powershell
.\.venv\Scripts\python.exe -c "import funasr; print(funasr.__version__)"
# 预期输出：1.4.19（或更高）
```

> ⚠️ 如果安装时出现 `[WinError 32] 另一个程序正在使用此文件`，说明有旧的 pip 进程在跑。
> 先关掉所有 pip/python 进程，再重新执行安装命令。

---

## 4. 下载模型

首次启动 `funasr-server` 时会**自动从 ModelScope 下载** SenseVoice-Small 模型，无需手动下载。

模型信息：
- 仓库：`iic/SenseVoiceSmall`
- 大小：约 936MB（`model.pt`）
- 缓存目录：`C:\Users\<你的用户名>\.cache\modelscope\models\iic--SenseVoiceSmall\`

下载速度取决于网络，通常 2-5 分钟。

> 如果想手动预下载，可以用 ModelScope SDK：
> ```python
> from modelscope import snapshot_download
> snapshot_download("iic/SenseVoiceSmall")
> ```

---

## 5. 启动语音识别服务

### 5.1 直接启动（测试用）

```powershell
.\.venv\Scripts\python.exe -m funasr.bin.server --model sensevoice --device cpu --port 8001
```

参数说明：

| 参数 | 值 | 说明 |
|---|---|---|
| `--model` | `sensevoice` | 使用 SenseVoice-Small 模型（FunASR 内置别名） |
| `--device` | `cpu` | 用 CPU 推理；有显卡可填 `cuda` |
| `--port` | `8001` | 服务监听端口 |

首次启动会下载模型并加载（约 10-30 秒），看到以下日志表示就绪：

```
INFO:     Uvicorn running on http://0.0.0.0:8001
```

### 5.2 启动脚本（推荐）

创建 `D:\models\speech\start-speech.ps1`：

```powershell
# SenseVoice-Small 语音识别服务启动脚本
# 接口：OpenAI Whisper 兼容，POST /v1/audio/transcriptions
$python = "d:\code\rag\know-engine-python\.venv\Scripts\python.exe"
& $python -m funasr.bin.server `
    --model sensevoice `
    --device cpu `
    --port 8001
```

以后开机后双击或在 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File D:\models\speech\start-speech.ps1
```

> ⚠️ 这个窗口不要关，关了语音识别就停了。需要长期运行可以用 `nssm` 注册成 Windows 服务。

---

## 6. 手动测试接口

### 6.1 查看可用模型

```powershell
curl http://127.0.0.1:8001/v1/models
```

预期返回：

```json
{
  "object": "list",
  "data": [
    {"id": "sensevoice", "object": "model"},
    {"id": "paraformer", "object": "model"},
    {"id": "fun-asr-nano", "object": "model"}
  ]
}
```

### 6.2 测试转写

用 FunASR 自带的中文示例音频（模型下载时自带）：

```powershell
$audio = "C:\Users\<你的用户名>\.cache\modelscope\models\iic--SenseVoiceSmall\snapshots\master\example\zh.mp3"

curl -X POST http://127.0.0.1:8001/v1/audio/transcriptions `
  -F "file=@$audio" `
  -F "model=sensevoice" `
  -F "response_format=json"
```

预期返回：

```json
{"text": "开饭时间早上九点至下午五点"}
```

> ✅ 到这一步，语音识别服务本身已经可用了。下面是接入「知识引擎」系统的步骤。

---

## 7. 接入「知识引擎」系统

### 7.1 改造语音服务代码

系统原有的 `app/services/speech.py` 用的是 **DashScope 专有格式**（`/chat/completions` + `input_audio` 字段），
和 FunASR 不兼容。需要改成 **OpenAI Whisper 通用接口**。

改造要点（已完成）：

- 请求地址：`POST {base_url}/v1/audio/transcriptions`
- 请求方式：`multipart/form-data`，字段 `file`（音频文件）+ `model`（模型名）
- 返回格式：`{"text": "转写结果"}`
- 本地端点（`provider=local`）免鉴权，不需要 API Key

完整代码见 [app/services/speech.py](file:///d:/code/rag/know-engine-python/app/services/speech.py)。

### 7.2 后台添加语音识别模型

进入后台 → **模型配置** → **新增模型**，填写：

| 字段 | 值 |
|---|---|
| 用途 | ✅ 语音识别 |
| 服务提供商 | 本地部署 |
| 模型展示名 | 本地 SenseVoice-Small |
| 模型名称 | `sensevoice` |
| 接口地址 | `http://127.0.0.1:8001` |
| API Key | （留空，本地免鉴权） |
| 启用 | ✅ |
| 设为默认 | ✅ |

保存后，进入 **模型用途** 页面，把「语音识别」绑定到这个模型。

> 也可以直接操作数据库（已完成）：
> ```sql
> INSERT INTO llm_model (purpose, provider, display_name, model_name, base_url, api_key, is_default, enabled, sort, remark)
> VALUES ('speech', 'local', '本地 SenseVoice-Small', 'sensevoice', 'http://127.0.0.1:8001', '', 1, 1, 0, 'FunASR SenseVoice-Small，本地 CPU 部署');
>
> UPDATE model_role_binding SET model_id = <新模型ID> WHERE role = 'speech';
> ```

### 7.3 验证端到端

用 Python 调用系统的 SpeechService：

```python
import asyncio
from app.services.speech import get_speech

async def main():
    with open("test.mp3", "rb") as f:
        audio = f.read()
    text = await get_speech().transcribe(audio, fmt="mp3")
    print(text)

asyncio.run(main())
```

预期输出转写文本，说明从「系统 → FunASR → 模型 → 返回」全链路通了。

---

## 8. 端口分工总览

| 端口 | 服务 | 用途 |
|---|---|---|
| 8009 | 知识引擎后端（uvicorn） | 主 API |
| 11434 | Ollama | 对话 / 向量模型 |
| 8080 | llama-server（bge-reranker） | 检索精排 |
| **8001** | **funasr-server（SenseVoice）** | **语音识别** |
| 9200 | Elasticsearch | 向量检索 |

---

## 9. 常见问题排查

### Q1：`funasr-server` 启动后端口没响应

**原因**：首次启动在下载模型（936MB），还没加载完。

**解决**：等 2-5 分钟，看日志是否出现 `Uvicorn running on http://0.0.0.0:8001`。

### Q2：转写返回空字符串

**原因**：音频格式不支持，或音频采样率不对。

**解决**：
- 优先用 wav / mp3 格式
- FunASR 支持 16kHz 采样率效果最好；其他采样率会自动重采样但可能降质
- 安装 ffmpeg 可支持更多格式：`winget install Gyan.FFmpeg`

### Q3：`ffmpeg is not installed` 警告

**影响**：torchaudio 仍可加载 wav/mp3，不影响基本功能。
**解决**（可选）：`winget install Gyan.FFmpeg`，支持更多音频格式。

### Q4：系统报「语音识别未配置」

**原因**：后台没有绑定「语音识别」用途的模型，或绑定的模型已停用。

**解决**：后台 → 模型用途 → 确认「语音识别」绑定了 SenseVoice-Small 且状态为启用。

### Q5：HTTP 404 `/v4/v1/audio/transcriptions`

**原因**：base_url 被错误地加了 `/v4` 前缀（DashScope 的路径）。

**解决**：确认后台模型的接口地址是 `http://127.0.0.1:8001`（不带任何路径后缀）。

### Q6：CPU 推理太慢

**优化方案**：
- 短音频（< 10 秒）一般 1-3 秒，可接受
- 长音频建议前端做 VAD（语音活动检测）切片后再发
- 有 NVIDIA 显卡可把 `--device cpu` 改成 `--device cuda`，速度提升 5-10 倍

---

## 10. 模型对比与切换

FunASR 还支持其他模型，启动时改 `--model` 参数即可：

| 模型 | --model 参数 | 特点 |
|---|---|---|
| SenseVoice-Small | `sensevoice` | 多语言、含情感、中文好（推荐） |
| Paraformer-large | `paraformer` | 中文长音频效果好 |
| Fun-ASR-Nano | `fun-asr-nano` | 超轻量，速度极快 |
| MOSS-Transcribe | `moss-transcribe-diarize` | 带说话人分离 |

切换后记得在后台把模型名称改成对应参数。

---

## 11. 开机自启（可选）

用 `nssm` 把 funasr-server 注册成 Windows 服务，开机自动启动：

```powershell
# 安装 nssm
winget install nssm

# 注册服务
nssm install SenseVoice "d:\code\rag\know-engine-python\.venv\Scripts\python.exe" "-m funasr.bin.server --model sensevoice --device cpu --port 8001"
nssm set SenseVoice AppDirectory "d:\code\rag\know-engine-python"
nssm start SenseVoice
```

---

## 附：关键文件索引

| 文件 | 作用 |
|---|---|
| `D:\models\speech\start-speech.ps1` | 语音识别启动脚本 |
| `app/services/speech.py` | 系统语音转写服务（OpenAI Whisper 兼容） |
| `app/modules/model/service.py` | 模型用途绑定解析 |
| `C:\Users\<你>\.cache\modelscope\models\iic--SenseVoiceSmall\` | 模型缓存目录 |

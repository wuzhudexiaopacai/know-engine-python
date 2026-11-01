"""文档格式转换服务。

转换策略（按优先级）：
1. 外部 MinerU 深度解析服务（PARSE__API_URL 配置时启用，PDF/Word 效果最佳）
2. 内置解析器降级链：pypdf / python-docx / openpyxl / 直读
   —— CPU 密集，统一走线程池，绝不阻塞事件循环

图片处理（内置解析器路径）：
- 抽取文档内图片原始字节，在 Markdown 中留 `![图片](__KE_IMG_n__)` 占位符
- 上层流水线把占位符替换成「视觉模型描述 + 对象存储 URL」，让图片内容可检索
- Excel 直接从 xlsx 包内 xl/media 读取，避免引入 Pillow 等额外依赖

工程细节：
- 转换产物统一为 Markdown 或 Excel 原始文件（Excel 直通，切片器单独处理）
- 外部服务超时/熔断/异常逐级降级，最终失败标记文档 FAILED 交补偿任务重试
- 内容 SHA-256 哈希贯穿版本去重
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from starlette.concurrency import run_in_threadpool

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import get_breaker

logger = get_logger("app.convert")

# Markdown 远程图片：只处理 http(s)，本地相对路径无法随单文件上传而无法解析
_REMOTE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def content_hash(path: Path) -> str:
    """流式 SHA-256（大文件不整读内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class ExtractedImage:
    """从文档中抽取的图片（原始字节 + 唯一占位符）。"""

    marker: str
    data: bytes
    mime: str
    label: str          # 位置说明，用于描述失败时的兜底 alt
    sha256: str


@dataclass
class ConvertResult:
    """转换结果：Markdown 产物（Excel 为 None）+ 抽取出的图片。"""

    path: Path | None
    images: list[ExtractedImage] = field(default_factory=list)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sniff_mime(data: bytes) -> str:
    """按魔数识别图片类型（不依赖 Pillow）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return "image/png"


class DocumentConverter:
    """文件 → Markdown 转换器。"""

    def __init__(self):
        cfg = get_settings().parse
        self._mineru_url = cfg.api_url
        self._timeout = cfg.timeout_seconds
        self._breaker = get_breaker("mineru", failure_threshold=3, recovery_seconds=120)

    async def convert(self, src: Path, file_type: str) -> ConvertResult:
        """返回转换产物与抽取出的图片。

        Excel/CSV 的 path 为 None（直通不转换），但 Excel 仍会抽取包内图片。
        失败抛异常，由上层事件监听器捕获并置 FAILED（补偿任务负责重试）。
        """
        if file_type in ("excel", "csv"):
            images = []
            if file_type == "excel":
                images = await run_in_threadpool(_excel_media_images, src)
            return ConvertResult(path=None, images=images)

        # 1. 外部 MinerU（可选）：其 Markdown 自带图片处理，不再二次抽取
        if self._mineru_url:
            try:
                return ConvertResult(
                    path=await self._breaker.call(self._mineru_convert, src, file_type),
                    images=[])
            except Exception as e:
                logger.warning("mineru_failed_fallback_builtin", error=str(e))

        # 2. 内置降级链
        out = src.with_suffix(".converted.md")
        if file_type == "pdf":
            text, images = await run_in_threadpool(self._pdf_to_markdown, src)
        elif file_type == "word":
            text, images = await run_in_threadpool(self._docx_to_markdown, src)
        elif file_type == "markdown":
            text, images = await self._markdown_with_images(src)
        else:
            text, images = await run_in_threadpool(_read_text, src), []
        out.write_text(text, encoding="utf-8")
        logger.info("converted_builtin", src=src.name, out=out.name,
                    chars=len(text), images=len(images))
        return ConvertResult(path=out, images=images)

    # ---------------- 网络图片（Markdown） ----------------
    async def _markdown_with_images(self, src: Path) -> tuple[str, list[ExtractedImage]]:
        """下载 Markdown 中的远程图片并改为本地占位符；失败保留原样。"""
        text = await run_in_threadpool(_read_text, src)
        cfg = get_settings().image
        if not cfg.parse_enabled:
            return text, []
        matches = list(_REMOTE_IMAGE_RE.finditer(text))[: cfg.max_images]
        if not matches:
            return text, []

        images: list[ExtractedImage] = []
        pieces: list[str] = []
        last = 0
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=5.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"}) as client:
            for match in matches:
                data = await _download_image(client, match.group(1), cfg.max_image_bytes)
                if not data:
                    continue
                marker = f"__KE_IMG_{len(images)}__"
                images.append(ExtractedImage(
                    marker=marker, data=data, mime=_sniff_mime(data),
                    label=f"Markdown 网络图片 {len(images) + 1}", sha256=_sha256(data)))
                pieces.append(text[last:match.start()])
                pieces.append(f"![图片]({marker})")
                last = match.end()
        if not images:
            return text, []
        pieces.append(text[last:])
        return "".join(pieces), images

    # ---------------- MinerU 外部服务 ----------------
    async def _mineru_convert(self, src: Path, file_type: str) -> Path:
        """调用 MinerU HTTP 接口：multipart 上传 → 取回 markdown 文本。"""
        with src.open("rb") as f:
            resp = await httpx.AsyncClient(timeout=self._timeout).post(
                f"{self._mineru_url}/api/v1/convert",
                files={"file": (src.name, f)},
                data={"output": "markdown"},
            )
        resp.raise_for_status()
        text = resp.json().get("result", {}).get("markdown") or resp.text
        out = src.with_suffix(".converted.md")
        out.write_text(text, encoding="utf-8")
        logger.info("converted_mineru", src=src.name, chars=len(text))
        return out

    # ---------------- 内置解析器 ----------------
    @staticmethod
    def _pdf_to_markdown(src: Path) -> tuple[str, list[ExtractedImage]]:
        """pypdf 逐页抽取文本与图片；损坏页跳过并计数（部分成功优于整体失败）。"""
        from pypdf import PdfReader

        reader = PdfReader(str(src))
        pages: list[str] = []
        images: list[ExtractedImage] = []
        limit = get_settings().image.max_images
        failed = 0
        for i, page in enumerate(reader.pages):
            try:
                block = (page.extract_text() or "").strip()
            except Exception as e:
                failed += 1
                block = ""
                logger.warning("pdf_page_parse_failed", page=i, error=str(e))
            try:
                page_images = list(page.images)
            except Exception as e:
                page_images = []
                logger.warning("pdf_images_parse_failed", page=i, error=str(e))
            for img in page_images:
                if len(images) >= limit:
                    break
                try:
                    data = img.data
                except Exception:
                    data = b""
                if not data:
                    continue
                marker = f"__KE_IMG_{len(images)}__"
                images.append(ExtractedImage(
                    marker=marker, data=data, mime=_sniff_mime(data),
                    label=f"PDF 第 {i + 1} 页", sha256=_sha256(data)))
                block += f"\n\n![图片]({marker})"
            if block.strip():
                pages.append(block)
        if failed:
            logger.info("pdf_partial", ok=len(pages), failed=failed)
        # PDF 无结构信息，输出为纯文本段落
        return "\n\n".join(pages), images

    @staticmethod
    def _docx_to_markdown(src: Path) -> tuple[str, list[ExtractedImage]]:
        """python-docx：标题层级 → Markdown #；表格 → GFM 表格；内嵌图片 → 占位符。"""
        import docx
        from docx.oxml.ns import qn

        d = docx.Document(str(src))
        lines: list[str] = []
        images: list[ExtractedImage] = []
        used_rids: set[str] = set()
        limit = get_settings().image.max_images

        def take(rid: str) -> str | None:
            """按关系 id 取图片字节，返回占位符；同一图片重复引用只登记一次。"""
            if rid in used_rids or len(images) >= limit:
                return None
            part = d.part.related_parts.get(rid)
            data = getattr(part, "blob", b"") if part is not None else b""
            if not data:
                return None
            used_rids.add(rid)
            marker = f"__KE_IMG_{len(images)}__"
            images.append(ExtractedImage(
                marker=marker, data=data, mime=_sniff_mime(data),
                label="Word 文档图片", sha256=_sha256(data)))
            return marker

        for para in d.paragraphs:
            style = para.style.name.lower()
            if style.startswith("heading"):
                try:
                    level = int(style.replace("heading", "").strip() or 1)
                except ValueError:
                    level = 1
                lines.append(f"{'#' * min(level, 6)} {para.text}")
            elif para.text.strip():
                lines.append(para.text)
            for blip in para._element.iter(qn("a:blip")):
                rid = blip.get(qn("r:embed"))
                marker = take(rid) if rid else None
                if marker:
                    lines.append(f"![图片]({marker})")

        for table in d.tables:
            rows = [[cell.text.replace("\n", " ") for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            lines.append(_to_markdown_table(rows))

        # 表格内、页眉页脚等未按段落引用的图片统一追加到文末，保证不丢图
        for rid, part in d.part.related_parts.items():
            if rid in used_rids or len(images) >= limit:
                continue
            content_type = getattr(part, "content_type", "") or ""
            if not content_type.startswith("image/"):
                continue
            data = getattr(part, "blob", b"")
            if not data:
                continue
            marker = f"__KE_IMG_{len(images)}__"
            images.append(ExtractedImage(
                marker=marker, data=data, mime=_sniff_mime(data),
                label="Word 文档图片", sha256=_sha256(data)))
            lines.append(f"![图片]({marker})")
        return "\n\n".join(lines), images


def _to_markdown_table(rows: list[list[str]]) -> str:
    header, *body = rows
    md = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    md += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(md)


def _read_text(src: Path) -> str:
    """文本/Markdown 直读（多编码尝试）。"""
    raw = src.read_bytes()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _excel_media_images(src: Path) -> list[ExtractedImage]:
    """从 xlsx 包内 xl/media 读取图片（不引入 Pillow，纯 zip 解析）。

    Excel 图片多是 logo/图表截图，且 openpyxl 读取图片依赖 Pillow；
    直接读 zip 既能覆盖需求，也不给部署增加依赖。
    """
    limit = get_settings().image.max_images
    images: list[ExtractedImage] = []
    try:
        with zipfile.ZipFile(src) as zf:
            for name in sorted(zf.namelist()):
                if not name.startswith("xl/media/"):
                    continue
                if Path(name).suffix.lower() not in _IMAGE_EXTENSIONS:
                    continue
                data = zf.read(name)
                if not data:
                    continue
                marker = f"__KE_IMG_{len(images)}__"
                images.append(ExtractedImage(
                    marker=marker, data=data, mime=_sniff_mime(data),
                    label=f"Excel 内嵌图片（{Path(name).name}）", sha256=_sha256(data)))
                if len(images) >= limit:
                    break
    except Exception as e:
        logger.warning("excel_media_parse_failed", error=str(e))
    return images


async def _download_image(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes | None:
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "").lower()
        if "image" not in content_type and not url.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return None
        data = resp.content
        if len(data) > max_bytes:
            return None
        return data
    except Exception as e:
        logger.warning("markdown_image_download_failed", url=url[:200], error=str(e))
        return None

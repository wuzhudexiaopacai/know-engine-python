"""文件类型检测。

红线：绝不信任扩展名，以文件头魔数为准，防止重命名绕过导致解析错乱。
支持：PDF / ZIP(xlsx) / OLE(xls) / DOCX / MD·TXT / CSV（文本类回退扩展名+内容双判定）。
"""

from __future__ import annotations

from pathlib import Path

# 魔数签名表
_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),          # xlsx / docx 均为 zip 容器，需二次判定
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "xls"),  # OLE2 复合文档
]

# 扩展名 → 归一化类型
_EXT_TYPES = {
    ".pdf": "pdf", ".md": "markdown", ".markdown": "markdown", ".txt": "text",
    ".xlsx": "excel", ".xls": "excel", ".csv": "csv", ".docx": "word", ".doc": "word",
}


def detect_type(path: Path, filename: str | None = None) -> str:
    """返回归一化类型：pdf / word / excel / csv / markdown / text / unknown。

    filename：原始文件名。分片合并后落地为 merged.bin，扩展名已丢失，
    需由调用方回传原始名，供文本类（md/txt/csv）的扩展名判定使用。
    二进制类型仍以魔数为准，扩展名仅作为文本类的辅助信号（不降低伪造防护）。
    """
    header = _read_header(path)

    for magic, kind in _MAGIC_SIGNATURES:
        if header.startswith(magic):
            if kind == "zip":
                return _detect_zip_inner(path)
            if kind == "xls":
                return "excel"
            return kind

    # 文本类：无二进制魔数。红线：扩展名不能单独定性（防二进制伪装 .md），
    # 必须先确认内容“确实是文本”，才能用原始扩展名归类；否则一律 unknown。
    # merged.bin 的 suffix 不可信，优先用回传的原始 filename 扩展名
    ext = (Path(filename).suffix.lower() if filename else "") or path.suffix.lower()
    if not _is_probably_text(header):
        return "unknown"
    # 内容确为文本：优先按已知文本扩展名归类
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext == ".csv":
        return "csv"
    if ext == ".txt":
        return "text"
    # 扩展名无法判定：回退内容嗅探（CSV 分隔符 / Markdown 语法），仍为文本则归 text
    if _looks_like_csv(header):
        return "csv"
    if _looks_like_markdown(header):
        return "markdown"
    return "text"


def _decode_head(head: bytes) -> str:
    """容错解码文件头：UTF-8 → GB18030 → Latin-1，绝不抛异常。

    GB18030 向下兼容 GBK/GB2312，覆盖中文 Windows 记事本另存为 ANSI 的 .md/.txt。

    红线：head 是按固定字节数截断出来的，末尾很可能只剩半个多字节字符，
    直接整段 decode 会抛 UnicodeDecodeError，进而被误降级成 latin-1 乱码，
    把纯文本文件判成 unknown。因此每种编码都先按"丢弃末尾 1~3 字节"重试。
    """
    for enc in ("utf-8", "gb18030"):
        for trim in range(4):
            chunk = head[:len(head) - trim] if trim else head
            try:
                return chunk.decode(enc)
            except UnicodeDecodeError:
                continue
    return head.decode("latin-1", "replace")


def _read_header(path: Path, size: int = 8192) -> bytes:
    """读文件头（BOM 自动剥离，支持 UTF-8/UTF-16LE/BE）。"""
    with path.open("rb") as f:
        head = f.read(size)
    if head.startswith(b"\xef\xbb\xbf"):
        return head[3:].decode("utf-8", "replace").encode()
    if head.startswith(b"\xff\xfe"):
        return head[2:].decode("utf-16-le", "replace").encode()[:size]
    if head.startswith(b"\xfe\xff"):
        return head[2:].decode("utf-16-be", "replace").encode()[:size]
    return head


def _detect_zip_inner(path: Path) -> str:
    """zip 容器二次判定：[Content_Types].xml 里查 word/excel 特征。"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if any(n.startswith("word/") for n in names):
                return "word"
            if any(n.startswith("xl/") for n in names):
                return "excel"
    except zipfile.BadZipFile:
        return "unknown"
    return "unknown"


def _looks_like_csv(head: bytes) -> bool:
    text = _decode_head(head)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 2 and all(("," in ln or "\t" in ln) for ln in lines[:5])


def _looks_like_markdown(head: bytes) -> bool:
    text = _decode_head(head)
    markers = ("# ", "## ", "- ", "**", "```", "|")
    return any(marker in line for line in text.splitlines()[:20] for marker in markers)


def _is_probably_text(head: bytes) -> bool:
    """可打印字符占比 > 85% 判为文本（控制字符容忍换行/制表）。"""
    if not head:
        return False
    text = _decode_head(head)
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable / max(len(text), 1) > 0.85

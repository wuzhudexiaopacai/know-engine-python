"""文档切片器家族。

策略矩阵：
| SMART / TITLE | MarkdownHeaderSplitter | 标题层级 + 父子分段 + 代码块保护 + 超长二次切割 |
| LENGTH        | LengthSplitter         | 按字符数，段落对齐，overlap 滑窗 |
| REGEX         | RegexSplitter          | 用户正则切分 |
| SEPARATOR     | SeparatorSplitter      | 用户分隔符切分 |
| excel/csv     | ExcelSplitter          | 键值对模式 / HTML 表格模式（行级完整性） |

父子分段协议（贯穿切片→存储→检索全链路，metadata 键见 MetaKey）：
- 超出 chunkSize 的章节保留完整父分段（skipEmbedding=1，不参与向量检索）
- 子分段携带 parentChunkId；检索命中子分段时回溯父分段全文替换
- 兄弟子分段共享 brotherChunkId，携带 index/total，检索时自动补全同级上下文
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.exceptions import ParamError
from app.models.constants import MetaKey, SplitType
from app.infra.snowflake import next_id_str


@dataclass
class Chunk:
    """切片产物：文本 + 元数据（入库时写 knowledge_segment.metadata）。"""

    text: str
    metadata: dict = field(default_factory=dict)


class TableMode(StrEnum):
    KV = "kv"          # 键值对模式：表头1：值1; 表头2：值2
    HTML = "html"      # HTML 表格模式：<table> 分块


# ==================================================================
# Markdown 标题切分（SMART / TITLE）
# ==================================================================
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# PDF/Word 转换产物常没有 # 标记，用编号标题补足（第X章 / 1. / 1.1 / 一、）
_NUMBERED_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百千0-9]+[章节部分篇步]"
    r"|[一二三四五六七八九十]+、"
    r"|[0-9]+(?:\.[0-9]+)+[.、．]?"
    r"|[0-9]+[.、．]\s)"
)
# 列表项/表格行不参与断行合并
_LIST_RE = re.compile(r"^(?:[-*+]\s|[0-9]+[.、)]\s)")
# 句末标点：命中即视为自然段结束
_SENT_END = ("。", "！", "？", "；", "：", ".", "!", "?", ";", ":", "…")
_FENCE_RE = re.compile(r"^(```|~~~)")


class MarkdownHeaderSplitter:
    """基于标题层级的 Parent-Child Chunking。

    工程细节：
    - ``` 围栏代码块整体视为原子单元，标题识别在代码块内失效
    - GFM 表格行不单独成段（与所属段落同段）
    - 章节超 chunk_size：保留父分段全文（skipEmbedding）+ 段落聚合子分段
    - title_level 限定"哪些层级作为独立分段边界"，更深层级并入正文
    """

    def __init__(self, chunk_size: int = 800, overlap: int = 0, title_level: int = 0):
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.title_level = title_level   # 0 = 不限制层级
        # 父分段封顶：整篇章节（如无标题的 PDF）不能作为一个 7 万字的父分段塞进模型
        self.parent_max_size = max(chunk_size * 5, 3000)

    def split(self, text: str, base_meta: dict) -> list[Chunk]:
        sections = self._parse_sections(self._reflow(text))
        chunks: list[Chunk] = []
        for order, (title_path, body) in enumerate(sections):
            body = body.strip()
            if not body:
                continue
            # 无显式标题时退回文档标题，避免所有分段 title 为空、引用无法辨认
            title = title_path or base_meta.get(MetaKey.TITLE) or ""
            meta = {**base_meta, MetaKey.TITLE: title, MetaKey.ORDER: order}
            if len(body) <= self.chunk_size:
                chunks.append(Chunk(body, {**meta, MetaKey.CHUNK_ID: next_id_str()}))
            else:
                chunks.extend(self._split_oversized(body, meta, order))
        return chunks

    # ---------------- 断行修复 ----------------
    def _reflow(self, text: str) -> str:
        """PDF 转换产物常把一句话硬断成多行且没有空行，导致整篇成为一个大块。

        这里把"未以句末标点结束"的连续行合并成自然段，标题/列表/表格/代码保持独立。
        已有良好空行分段的 Markdown 不受影响（空行仍触发 flush）。
        """
        out: list[str] = []
        buf: list[str] = []
        in_fence = False

        def flush():
            if buf:
                out.append("".join(buf))
                buf.clear()

        def append_piece(piece: str):
            if buf and (buf[-1][-1:].isascii() or piece[:1].isascii()):
                buf.append(" ")
            buf.append(piece)

        for line in text.splitlines():
            s = line.strip()
            if _FENCE_RE.match(s):
                flush()
                out.append(line)
                in_fence = not in_fence
                continue
            if in_fence:
                out.append(line)
                continue
            if not s:
                flush()
                out.append("")
                continue
            if (self._heading_of(s) is not None or s.startswith("|")
                    or _LIST_RE.match(s)):
                flush()
                out.append(line)
                continue
            append_piece(s)
            if s.endswith(_SENT_END):
                flush()
        flush()
        return "\n".join(out)

    @staticmethod
    def _heading_of(line: str) -> tuple[int, str] | None:
        """识别 Markdown # 标题与编号标题，返回 (层级, 标题)。"""
        m = _HEADING_RE.match(line)
        if m:
            return len(m.group(1)), m.group(2).strip()
        if _NUMBERED_RE.match(line):
            text = line.strip()
            level = 2
            if text.startswith("第"):
                level = 1
            else:
                dots = text.split(maxsplit=1)[0].count(".") if " " in text else text.count(".")
                level = min(dots + 1, 6) if dots else 2
            return level, text
        return None

    # ---------------- 章节解析 ----------------
    def _parse_sections(self, text: str) -> list[tuple[str, str]]:
        """扫描行 → [(标题路径, 正文)]。维护标题栈生成层级路径。"""
        lines = text.splitlines()
        heading_stack: list[tuple[int, str]] = []   # (level, title)
        sections: list[tuple[str, str]] = []
        buf: list[str] = []
        in_fence = False

        def flush(path: str):
            if buf and any(x.strip() for x in buf):
                sections.append((path, "\n".join(buf)))

        current_path = ""
        for line in lines:
            if _FENCE_RE.match(line.strip()):
                in_fence = not in_fence

            heading = None if in_fence else self._heading_of(line)
            if heading is not None:
                level, title = heading
                # 深层级标题（超过 title_level 限制）不作为分段边界
                if self.title_level and level > self.title_level:
                    buf.append(line)
                    continue
                flush(current_path)
                buf.clear()
                # 出栈至当前层级，形成祖先链
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                current_path = " > ".join(t for _, t in heading_stack)
            else:
                buf.append(line)
        flush(current_path)
        return sections

    # ---------------- 超长章节：父 + 子 ----------------
    def _split_oversized(self, body: str, meta: dict, order: int) -> list[Chunk]:
        """父分段全文（skipEmbedding=1）+ 段落聚合子分段（携带父子/兄弟链）。

        超长章节（常见于无标题的 PDF 整篇正文）按 parent_max_size 再切分为多个父分段，
        避免单个父分段达到几万字、检索命中后整篇注入 Prompt 挤爆上下文。
        """
        result: list[Chunk] = []
        for part in self._split_parents(body):
            parent_id = next_id_str()
            result.append(Chunk(part, {
                **meta, MetaKey.CHUNK_ID: parent_id, MetaKey.SKIP_EMBEDDING: 1,
            }))
            blocks = self._aggregate_blocks(part)
            brother_id = next_id_str()
            result.extend(Chunk(b, {
                **meta, MetaKey.CHUNK_ID: next_id_str(),
                MetaKey.PARENT_CHUNK_ID: parent_id, MetaKey.SKIP_EMBEDDING: 0,
                MetaKey.BROTHER_CHUNK_ID: brother_id,
                MetaKey.BROTHER_INDEX: i, MetaKey.BROTHER_TOTAL: len(blocks),
            }) for i, b in enumerate(blocks))
        return result

    def _split_parents(self, body: str) -> list[str]:
        """按段落边界把超长正文切成不超过 parent_max_size 的父分段。"""
        blocks = self._aggregate_blocks(body)
        parts: list[str] = []
        cur: list[str] = []
        size = 0
        for blk in blocks:
            if cur and size + len(blk) > self.parent_max_size:
                parts.append("\n\n".join(cur))
                cur, size = [], 0
            cur.append(blk)
            size += len(blk) + 2
        if cur:
            parts.append("\n\n".join(cur))
        return parts or [body]

    def _aggregate_blocks(self, body: str) -> list[str]:
        """段落聚合：空行分段；代码块/表格原子保留；块间按 overlap 保留尾段。"""
        raw_blocks: list[str] = []
        buf: list[str] = []
        in_fence = False
        for line in body.splitlines():
            if _FENCE_RE.match(line.strip()):
                in_fence = not in_fence
                buf.append(line)
                continue
            if in_fence:
                buf.append(line)
            elif not line.strip():
                if buf:
                    raw_blocks.append("\n".join(buf))
                    buf = []
            else:
                buf.append(line)
        if buf:
            raw_blocks.append("\n".join(buf))

        # 聚合到 chunk_size；单块超限（超长表格）保持原子，宁大不截
        result: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for blk in raw_blocks:
            if cur and cur_len + len(blk) > self.chunk_size:
                result.append("\n\n".join(cur))
                # overlap：保留上一块尾部段落作为新块起点
                overlap_blocks: list[str] = []
                o_len = 0
                for b in reversed(cur):
                    if o_len + len(b) > self.overlap:
                        break
                    overlap_blocks.insert(0, b)
                    o_len += len(b)
                cur = overlap_blocks
                cur_len = o_len
            cur.append(blk)
            cur_len += len(blk) + 2
        if cur:
            result.append("\n\n".join(cur))
        return result


# ==================================================================
# 按长度 / 正则 / 分隔符
# ==================================================================
class LengthSplitter:
    """按字符数切分：优先在段落边界断开，overlap 滑窗回退。"""

    def __init__(self, chunk_size: int = 800, overlap: int = 100):
        self.chunk_size = chunk_size
        self.overlap = min(overlap, chunk_size // 2)

    def split(self, text: str, base_meta: dict) -> list[Chunk]:
        chunks: list[Chunk] = []
        start, order = 0, 0
        n = len(text)
        while start < n:
            end = min(start + self.chunk_size, n)
            if end < n:
                # 尝试在最近的段落/句子边界截断，避免语义截断
                window = text[start:end]
                cut = max(window.rfind("\n\n"), window.rfind("。"), window.rfind("."))
                if cut > self.chunk_size // 4:
                    end = start + cut + 1
            piece = text[start:end].strip()
            if piece:
                chunks.append(Chunk(piece, {
                    **base_meta, MetaKey.CHUNK_ID: next_id_str(), MetaKey.ORDER: order}))
                order += 1
            if end >= n:
                break
            start = max(end - self.overlap, start + 1)
        return chunks


class RegexSplitter:
    """用户正则切分（re.split，空片段丢弃）。"""

    def __init__(self, pattern: str):
        if not pattern:
            raise ParamError("REGEX 模式必须提供 regex 参数")
        try:
            self._re = re.compile(pattern, re.MULTILINE)
        except re.error as e:
            raise ParamError(f"正则表达式非法: {e}") from e

    def split(self, text: str, base_meta: dict) -> list[Chunk]:
        parts = [p.strip() for p in self._re.split(text) if p and p.strip()]
        return [Chunk(p, {**base_meta, MetaKey.CHUNK_ID: next_id_str(),
                          MetaKey.ORDER: i}) for i, p in enumerate(parts)]


class SeparatorSplitter:
    """固定分隔符切分。"""

    def __init__(self, separator: str):
        if not separator:
            raise ParamError("SEPARATOR 模式必须提供 separator 参数")
        self.separator = separator

    def split(self, text: str, base_meta: dict) -> list[Chunk]:
        parts = [p.strip() for p in text.split(self.separator) if p and p.strip()]
        return [Chunk(p, {**base_meta, MetaKey.CHUNK_ID: next_id_str(),
                          MetaKey.ORDER: i}) for i, p in enumerate(parts)]


# ==================================================================
# Excel / CSV 双模式（RAGFlow 风格）
# ==================================================================
class ExcelSplitter:
    """结构化表格 → 可检索文本。

    - KV 模式（默认）：每行自包含语义 `表头1：值1; 表头2：值2`，适合向量检索
    - HTML 模式：按 chunk_size 分块输出 <table>，每块自动携带表头；
      行级完整性：同一行绝不拆分到不同分块，单行超限也至少保一行
    """

    def __init__(self, mode: TableMode = TableMode.KV, chunk_size: int = 800):
        self.mode = mode
        self.chunk_size = chunk_size

    def split_sheet(self, rows: list[list[str]], base_meta: dict) -> list[Chunk]:
        rows = [[(c or "").strip() for c in r] for r in rows if any((c or "").strip() for c in r)]
        if len(rows) < 2:
            return []
        header, body = rows[0], rows[1:]
        if self.mode == TableMode.KV:
            return self._split_kv(header, body, base_meta)
        return self._split_html(header, body, base_meta)

    @staticmethod
    def load_rows(path, file_type: str) -> list[list[str]]:
        """读表格文件为二维字符串数组（xlsx 用 openpyxl，csv 用标准库）。"""
        if file_type == "csv":
            import csv
            import io

            raw = path.read_bytes()
            for enc in ("utf-8-sig", "gb18030", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", "replace")
            return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]

        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        rows: list[list[str]] = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else str(v) for v in row])
            rows.append([""])          # 多 sheet 间空行分隔，避免跨 sheet 聚合
        wb.close()
        return rows

    def _split_kv(self, header: list[str], body: list[str], base_meta: dict) -> list[Chunk]:
        chunks, buf, buf_len, order = [], [], 0, 0

        def flush():
            nonlocal buf, buf_len, order
            if buf:
                chunks.append(Chunk("\n".join(buf), {
                    **base_meta, MetaKey.CHUNK_ID: next_id_str(), MetaKey.ORDER: order}))
                order += 1
                buf, buf_len = [], 0

        for r in body:
            line = "; ".join(f"{h}：{v}" for h, v in zip(header, r) if v)
            if not line:
                continue
            if buf and buf_len + len(line) > self.chunk_size:
                flush()
            buf.append(line)
            buf_len += len(line) + 1
        flush()
        return chunks

    def _split_html(self, header: list[str], body: list[list[str]], base_meta: dict) -> list[Chunk]:
        thead = "<tr>" + "".join(f"<th>{h}</th>" for h in header) + "</tr>"
        chunks, rows_buf, cur_len, order = [], [], len(thead), 0

        def flush():
            nonlocal rows_buf, cur_len, order
            if rows_buf:
                table = f"<table>{thead}{''.join(rows_buf)}</table>"
                chunks.append(Chunk(table, {
                    **base_meta, MetaKey.CHUNK_ID: next_id_str(), MetaKey.ORDER: order}))
                order += 1
                rows_buf, cur_len = [], len(thead)

        for r in body:
            tr = "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
            # 行级完整性：加入该行会超限且已有行 → 先 Flush；单行超限仍保证"至少一行"
            if rows_buf and cur_len + len(tr) > self.chunk_size:
                flush()
            rows_buf.append(tr)
            cur_len += len(tr)
        flush()
        return chunks


# ==================================================================
# 工厂（对应 DocumentSplitterFactory）
# ==================================================================
def create_splitter(split_type: str, *, chunk_size: int = 800, overlap: int = 0,
                   title_level: int = 0, regex: str | None = None,
                   separator: str | None = None):
    """按切片策略实例化对应切片器。"""
    st = SplitType(split_type)
    if st in (SplitType.TITLE, SplitType.SMART):
        # SMART：自动 overlap = chunkSize × 10%
        ov = overlap or (chunk_size // 10 if st == SplitType.SMART else 0)
        return MarkdownHeaderSplitter(chunk_size=chunk_size, overlap=ov, title_level=title_level)
    if st == SplitType.LENGTH:
        return LengthSplitter(chunk_size=chunk_size, overlap=overlap)
    if st == SplitType.REGEX:
        return RegexSplitter(regex or "")
    if st == SplitType.SEPARATOR:
        return SeparatorSplitter(separator or "")
    raise ParamError(f"不支持的切片策略: {split_type}")

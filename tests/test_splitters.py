"""文档切片器单元测试。

覆盖五种切片策略与工厂分发：
- MarkdownHeaderSplitter（TITLE/SMART）：标题分段、代码块保护、超长父子分段、层级限制
- LengthSplitter：定长切分、段落边界优先、overlap 滑窗
- RegexSplitter / SeparatorSplitter：自定义规则切分与参数校验
- ExcelSplitter：KV / HTML 双模式、行级完整性
"""

import pytest

from app.core.exceptions import ParamError
from app.models.constants import MetaKey
from app.modules.document.splitters import (
    ExcelSplitter,
    LengthSplitter,
    MarkdownHeaderSplitter,
    RegexSplitter,
    SeparatorSplitter,
    TableMode,
    create_splitter,
)

BASE_META = {MetaKey.DOC_ID: "doc-1", MetaKey.TITLE: "测试文档"}


class TestMarkdownHeaderSplitter:
    """标题层级切分（父子分段协议）。"""

    def test_split_by_headings(self):
        text = "# 章节一\n\n这是章节一的正文内容。\n\n# 章节二\n\n这是章节二的正文内容。"
        chunks = MarkdownHeaderSplitter(chunk_size=800).split(text, dict(BASE_META))
        assert len(chunks) == 2
        assert chunks[0].metadata[MetaKey.TITLE] == "章节一"
        assert chunks[1].metadata[MetaKey.TITLE] == "章节二"
        # 标题路径前置进正文：向量与全文检索都能命中主题词
        assert chunks[0].text == "章节一\n这是章节一的正文内容。"
        # 正文本身以标题开头时不重复前置（_compose 防冗余）
        dup = MarkdownHeaderSplitter(chunk_size=800).split(
            "# 章节一\n\n章节一的正文内容。", dict(BASE_META))
        assert dup[0].text == "章节一的正文内容。"

    def test_heading_inside_code_fence_not_split(self):
        """围栏代码块整体原子保留，块内 # 行不识别为标题。"""
        text = ("# 真正的标题\n\n正文内容。\n\n"
                "```\n# 代码里的注释不是标题\nprint('hi')\n```")
        chunks = MarkdownHeaderSplitter(chunk_size=800).split(text, dict(BASE_META))
        assert len(chunks) == 1
        assert "# 代码里的注释不是标题" in chunks[0].text

    def test_oversized_section_parent_child(self):
        """超长章节：父分段 skipEmbedding=1 + 子分段携带父子/兄弟链。"""
        para = "这是一段用于凑长度的正文。" * 6          # 每段 78 字
        text = "# 长章节\n\n" + "\n\n".join([para, para, para])
        chunks = MarkdownHeaderSplitter(chunk_size=100).split(text, dict(BASE_META))

        parents = [c for c in chunks if c.metadata.get(MetaKey.SKIP_EMBEDDING) == 1]
        children = [c for c in chunks if c.metadata.get(MetaKey.SKIP_EMBEDDING) == 0]
        assert len(parents) == 1
        assert len(children) >= 2

        # 父分段保留全文（含标题前置），不参与向量检索
        parent_id = parents[0].metadata[MetaKey.CHUNK_ID]
        assert para in parents[0].text

        # 子分段全部回指父分段，且共享同一个兄弟分组
        for c in children:
            assert c.metadata[MetaKey.PARENT_CHUNK_ID] == parent_id
        brother_ids = {c.metadata[MetaKey.BROTHER_CHUNK_ID] for c in children}
        assert len(brother_ids) == 1
        assert {c.metadata[MetaKey.BROTHER_TOTAL] for c in children} == {len(children)}
        assert sorted(c.metadata[MetaKey.BROTHER_INDEX] for c in children) == \
            list(range(len(children)))

    def test_title_level_limits_boundary(self):
        """title_level=1 时二级标题并入正文，不再作为分段边界。"""
        text = "# 一级\n\n一级正文。\n\n## 二级\n\n二级正文。"
        chunks = MarkdownHeaderSplitter(chunk_size=800, title_level=1) \
            .split(text, dict(BASE_META))
        assert len(chunks) == 1
        assert "二级正文。" in chunks[0].text

    def test_no_heading_falls_back_to_doc_title(self):
        """无标题正文的 metadata 标题回退为文档标题，保证引用可辨认。"""
        text = "没有标题的正文内容，长度足够超过合并阈值四十个字符，所以不会被并段处理掉，保留为独立分段。"
        chunks = MarkdownHeaderSplitter(chunk_size=800).split(text, dict(BASE_META))
        assert len(chunks) == 1
        assert chunks[0].metadata[MetaKey.TITLE] == "测试文档"

    def test_order_is_globally_sequential(self):
        """分段序号全局连续：检索侧按 order ±1 取相邻段依赖此约定。"""
        text = "# 一\n\n内容一。\n\n# 二\n\n内容二。\n\n# 三\n\n内容三。"
        chunks = MarkdownHeaderSplitter(chunk_size=800).split(text, dict(BASE_META))
        assert [c.metadata[MetaKey.ORDER] for c in chunks] == list(range(len(chunks)))


class TestLengthSplitter:
    """定长切分。"""

    def test_short_text_single_chunk(self):
        chunks = LengthSplitter(chunk_size=100).split("短文本", dict(BASE_META))
        assert len(chunks) == 1
        assert chunks[0].text == "短文本"
        assert chunks[0].metadata[MetaKey.CHUNK_ID]

    def test_long_text_splits_with_overlap(self):
        """无段落边界时按定长硬切，相邻块保持 overlap 重叠。"""
        chunks = LengthSplitter(chunk_size=100, overlap=20).split("字" * 250, dict(BASE_META))
        assert len(chunks) == 3
        for c in chunks:
            assert len(c.text) <= 100
        # 前一块尾部 20 字 == 后一块头部 20 字
        assert chunks[0].text[-20:] == chunks[1].text[:20]

    def test_prefers_paragraph_boundary(self):
        """优先在段落/句读边界断开（\n\n 与 。 都算），避免语义截断。"""
        first = "第一段内容，" + "字" * 88 + "。"          # 95 字，接近 chunk_size
        text = first + "\n\n" + "第二段内容。" + "字" * 60
        chunks = LengthSplitter(chunk_size=100, overlap=0).split(text, dict(BASE_META))
        assert chunks[0].text == first                     # 在第一段末尾的句号/空行处断开
        assert chunks[1].text.startswith("第二段内容。")


class TestRegexSplitter:
    def test_regex_split(self):
        chunks = RegexSplitter(r"===").split("第一部分===第二部分===第三部分", dict(BASE_META))
        assert [c.text for c in chunks] == ["第一部分", "第二部分", "第三部分"]
        assert [c.metadata[MetaKey.ORDER] for c in chunks] == [0, 1, 2]

    def test_empty_pattern_raises(self):
        with pytest.raises(ParamError):
            RegexSplitter("")

    def test_invalid_pattern_raises(self):
        with pytest.raises(ParamError):
            RegexSplitter("([")


class TestSeparatorSplitter:
    def test_separator_split(self):
        chunks = SeparatorSplitter("|").split("a|b||c", dict(BASE_META))
        assert [c.text for c in chunks] == ["a", "b", "c"]   # 空片段丢弃

    def test_empty_separator_raises(self):
        with pytest.raises(ParamError):
            SeparatorSplitter("")


class TestExcelSplitter:
    """结构化表格切分：KV 模式 / HTML 模式。"""

    ROWS = [["姓名", "部门", "职级"],
            ["张三", "研发部", "P7"],
            ["李四", "产品部", "P8"]]

    def test_kv_mode_row_as_sentence(self):
        """每行生成自包含语义文本 `表头：值`，适合向量检索。"""
        chunks = ExcelSplitter(mode=TableMode.KV).split_sheet(self.ROWS, dict(BASE_META))
        assert len(chunks) == 1
        assert "姓名：张三" in chunks[0].text
        assert "部门：研发部" in chunks[0].text
        assert "姓名：李四" in chunks[0].text

    def test_kv_mode_respects_chunk_size(self):
        chunks = ExcelSplitter(mode=TableMode.KV, chunk_size=30) \
            .split_sheet(self.ROWS, dict(BASE_META))
        assert len(chunks) == 2
        assert "张三" in chunks[0].text
        assert "李四" in chunks[1].text

    def test_html_mode_keeps_header_and_row_integrity(self):
        """HTML 模式：每个分块自动携带表头；同一行绝不拆分到不同分块。"""
        rows = [["h1", "h2"]] + [[f"r{i}c1", f"r{i}c2"] for i in range(20)]
        chunks = ExcelSplitter(mode=TableMode.HTML, chunk_size=80) \
            .split_sheet(rows, dict(BASE_META))
        assert len(chunks) > 1
        for c in chunks:
            assert c.text.startswith("<table>") and c.text.endswith("</table>")
            assert "<th>h1</th>" in c.text            # 表头随行块重复
            assert c.text.count("<tr>") >= 2          # 表头行 + 至少一行数据

    def test_header_only_returns_empty(self):
        assert ExcelSplitter().split_sheet([["仅表头"]], dict(BASE_META)) == []


class TestCreateSplitter:
    """工厂分发。"""

    def test_dispatch(self):
        assert isinstance(create_splitter("TITLE"), MarkdownHeaderSplitter)
        assert isinstance(create_splitter("LENGTH", chunk_size=100, overlap=10), LengthSplitter)
        assert isinstance(create_splitter("REGEX", regex="=="), RegexSplitter)
        assert isinstance(create_splitter("SEPARATOR", separator="|"), SeparatorSplitter)

    def test_smart_auto_overlap(self):
        """SMART 策略自动 overlap = chunkSize × 10%。"""
        splitter = create_splitter("SMART", chunk_size=100)
        assert isinstance(splitter, MarkdownHeaderSplitter)
        assert splitter.overlap == 10

    def test_regex_without_pattern_raises(self):
        with pytest.raises(ParamError):
            create_splitter("REGEX")

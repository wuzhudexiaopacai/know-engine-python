"""MCP 客户端与工具编排测试（不联网、不拉起子进程）。"""

import asyncio

from app.config.settings import McpConfig
from app.infra import mcp_client as mc
from app.services import mcp_agent


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, texts, is_error=False):
        self.content = [_Block(t) for t in texts]
        self.isError = is_error


class TestToolSpec:
    def test_qualified_name_is_openai_safe(self):
        assert mc._qualified("my search", "web.search") == "my_search__web_search"

    def test_to_openai_shape(self):
        tool = mc.McpTool(server="s", name="t", qualified="s__t",
                          description="搜索", input_schema={"type": "object"})
        spec = tool.to_openai()
        assert spec["type"] == "function"
        assert spec["function"]["name"] == "s__t"
        assert spec["function"]["parameters"] == {"type": "object"}


class TestDisabled:
    def test_default_config_disabled(self, monkeypatch):
        # 测试环境可能注入了 MCP__ENABLED，这里显式归零验证默认行为
        monkeypatch.setenv("MCP__ENABLED", "false")
        monkeypatch.setenv("MCP__SERVERS", "[]")
        client = mc.McpClient(McpConfig())
        assert client.enabled is False
        assert asyncio.run(client.list_tools()) == []

    def test_enabled_without_servers_is_disabled(self):
        assert mc.McpClient(McpConfig(enabled=True, servers=[])).enabled is False


class TestResultParsing:
    def test_joins_text_blocks(self):
        client = mc.McpClient(McpConfig(max_result_chars=100))
        assert client._result_text(_Result(["第一段", "第二段"])) == "第一段\n第二段"

    def test_truncates_over_limit(self):
        client = mc.McpClient(McpConfig(max_result_chars=5))
        text = client._result_text(_Result(["0123456789"]))
        assert text.startswith("01234")
        assert "已截断" in text

    def test_empty_result_has_placeholder(self):
        assert mc.McpClient(McpConfig())._result_text(_Result([])) == "（工具未返回内容）"


class TestTimeSensitive:
    def test_date_question_is_time_sensitive(self):
        assert mcp_agent.is_time_sensitive("今天是几月几日") is True

    def test_latest_question_is_time_sensitive(self):
        assert mcp_agent.is_time_sensitive("Python 最新版本") is True

    def test_plain_technical_question_is_not(self):
        assert mcp_agent.is_time_sensitive("什么是倒排索引") is False


class TestTrigger:
    def test_empty_chunks_always_uses_tools(self):
        assert mcp_agent.should_use_tools("随便问", []) is True

    def test_fresh_keyword_triggers(self):
        assert mcp_agent.should_use_tools("最新的 Python 版本是多少", [object()]) is True

    def test_plain_question_skips(self):
        assert mcp_agent.should_use_tools("什么是倒排索引", [object()]) is False


class TestClockQuestion:
    def test_date_question_is_clock(self):
        assert mcp_agent.is_clock_question("今天是几月几日") is True

    def test_weekday_question_is_clock(self):
        assert mcp_agent.is_clock_question("今天星期几") is True

    def test_news_question_is_not_clock(self):
        assert mcp_agent.is_clock_question("今天有什么新闻") is False

    def test_clock_question_skips_network(self, monkeypatch):
        fake = _FakeMcp()
        monkeypatch.setattr(mcp_agent, "get_mcp", lambda: fake)
        out = asyncio.run(mcp_agent.augment("今天是几月几日", "", llm=_FakeLlm(),
                                            chunks=[]))
        assert out.empty
        assert fake.calls == []


class TestSearchToolPicking:
    def test_ignores_slow_marks_in_description(self):
        """搜索工具的描述里常提到 tavily_extract，不能因此被判定为慢工具。"""
        tools = [
            mc.McpTool(server="s", name="tavily_extract", qualified="s__tavily_extract",
                       description="Extract content from URLs", input_schema={}),
            mc.McpTool(server="s", name="tavily_search", qualified="s__tavily_search",
                       description="Search the web; read a page with tavily_extract",
                       input_schema={}),
        ]
        assert mcp_agent._pick_search_tool(tools).qualified == "s__tavily_search"

    def test_skips_slow_tool_by_name(self):
        tools = [mc.McpTool(server="s", name="tavily_research",
                            qualified="s__tavily_research",
                            description="Perform comprehensive research", input_schema={})]
        assert mcp_agent._pick_search_tool(tools) is None


class _FakeMcp:
    enabled = True

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return [mc.McpTool(server="s", name="search", qualified="s__search",
                           description="联网搜索", input_schema={"type": "object"})]

    async def call(self, tool, arguments):
        self.calls.append((tool.qualified, arguments))
        return "搜索结果：今日天气晴"


class _FakeLlm:
    def __init__(self):
        self.rounds = 0

    async def complete_with_tools(self, messages, tools, **_):
        self.rounds += 1
        if self.rounds == 1:
            return "", [{"id": "c1", "name": "s__search",
                         "arguments": '{"q": "今天天气"}'}]
        return "已获取天气信息", []


class TestAugment:
    def test_runs_tool_and_injects_result(self, monkeypatch):
        fake = _FakeMcp()
        monkeypatch.setattr(mcp_agent, "get_mcp", lambda: fake)
        out = asyncio.run(mcp_agent.augment("今天天气怎么样", "", llm=_FakeLlm(),
                                            chunks=[object()]))
        assert "s__search" in out.used
        assert "搜索结果：今日天气晴" in out.text
        assert fake.calls == [("s__search", {"q": "今天天气"})]

    def test_skips_when_retrieval_is_enough(self, monkeypatch):
        fake = _FakeMcp()
        monkeypatch.setattr(mcp_agent, "get_mcp", lambda: fake)
        out = asyncio.run(mcp_agent.augment("什么是倒排索引", "", llm=_FakeLlm(),
                                            chunks=[object()]))
        assert out.empty
        assert fake.calls == []

    def test_invalid_arguments_fall_back_to_empty(self):
        assert mcp_agent._parse_args("not-json") == {}
        assert mcp_agent._parse_args("[1,2]") == {}
        assert mcp_agent._parse_args('{"q":"x"}') == {"q": "x"}

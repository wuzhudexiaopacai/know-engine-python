"""MCP 工具编排：检索后按需调用外部工具（联网搜索等）补充资料。

触发策略（控制成本与时延）：
- 知识库检索为空（长尾问题）→ 联网补充；
- 问题含时效性词（最新/今天/新闻/版本/价格…）→ 联网补充；
- 其余情况不进入工具循环：多轮 tool call 会明显增加首 token 时延。

流程：tools/list → LLM 带 tools 决策 → 执行 tool_calls → 结果回填 → 最多 max_rounds 轮。
任何外部异常都返回空结果，生成阶段照常用知识库资料。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.mcp_client import get_mcp

logger = get_logger("app.mcp.agent")

# 时效性关键词：命中即认为需要外部实时信息（也用于跳过 QA 缓存）
FRESH_HINTS = ("最新", "今天", "今日", "当前", "现在", "近期", "刚刚", "新闻",
               "发布", "版本", "价格", "股价", "天气", "汇率", "今年",
               "几月几日", "几号", "星期几", "几点")

_SYSTEM = (
    "你可以调用工具获取外部信息（如联网搜索）。规则：\n"
    "1. 仅在现有资料不足、问题涉及时效性或需要实时数据时调用工具；\n"
    "2. 一次只调用必要的工具，参数要具体（关键词、时间范围）；\n"
    "3. 拿到足够信息后直接给出中文结论，不要重复调用；\n"
    "4. 引用工具结果时保留来源链接。"
)


@dataclass
class ToolAugment:
    """工具补充结果：text 注入生成 Prompt，used 记录调用过的工具，
    sources 为可供前端展示、点击跳转的外部来源（网页搜索结果）。"""

    text: str = ""
    used: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text


def enabled() -> bool:
    """MCP 工具是否启用：未配置时对话链路不产生任何额外调用与提示。"""
    return get_mcp().enabled


def is_time_sensitive(question: str) -> bool:
    """问题是否依赖实时信息（日期、版本、价格、新闻…）。

    这类问题不能命中 QA 缓存：缓存里是历史答案，越久越错。
    """
    return any(h in (question or "") for h in FRESH_HINTS)


def should_use_tools(question: str, chunks: list) -> bool:
    """是否需要进入工具循环：检索为空，或问题有时效性诉求。"""
    if not chunks:
        return True
    return is_time_sensitive(question)


# 只问「今天几号/几点」这类问题：本地时钟就是权威答案，联网搜索反而可能拿到
# 被搜索引擎缓存的旧页面（实测某日历站仍是 11 月 1 日），把答案带偏。
_CLOCK_HINTS = ("几月几日", "几月几号", "几号", "几号了", "星期几", "周几",
                "几点", "多少号", "什么日期", "今天日期", "当前日期", "现在时间")


def is_clock_question(question: str) -> bool:
    """是否只是询问当前日期/时间：由 system prompt 注入的本机时间直接回答。"""
    q = question or ""
    return any(h in q for h in _CLOCK_HINTS) and not any(
        h in q for h in ("新闻", "版本", "价格", "股价", "汇率", "天气"))


_SEARCH_MARKS = ("search", "搜索")
# 抽取/爬取/深度研究类工具耗时长或需要 URL，不适合作为时效性问题的默认搜索
_SLOW_MARKS = ("extract", "crawl", "map", "research")


def _pick_search_tool(tools) -> McpTool | None:
    """挑选一次即可返回结果的搜索工具。

    慢工具只按「工具名」排除：描述里常顺带提及别的工具（如 tavily_search
    的说明里写着"read a page with tavily_extract"），按描述匹配会误杀搜索工具。
    """
    for t in tools:
        name = t.name.lower()
        if any(m in name for m in _SLOW_MARKS):
            continue
        hay = f"{name} {t.description or ''}".lower()
        if any(m in hay for m in _SEARCH_MARKS):
            return t
    return None


def _direct_args(tool: McpTool, question: str) -> dict | None:
    """按常见参数名填充关键词；找不到合适参数时返回 None，交给模型决策。"""
    schema = tool.input_schema or {}
    props = schema.get("properties") or {}
    for name in ("query", "q", "search_query", "keyword", "keywords"):
        if name in props:
            return {name: question}
    for name in schema.get("required") or []:
        if (props.get(name) or {}).get("type") == "string":
            return {name: question}
    return None


def _failed(output: str) -> bool:
    """McpClient.call 失败时返回「（工具 …）」说明串，这类结果不能当资料注入。"""
    return output.startswith("（工具 ")


# 搜索结果一般是 markdown 或 JSON 文本：优先取 [标题](链接)，退化到裸链接
_MD_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"https?://[^\s\"')\]，。；]+")
_MD_NOISE = re.compile(r"[*`#>]+")


def _clean(text: str) -> str:
    """去掉 markdown 装饰符与多余空白，得到可直接展示的标题/摘要。

    不剥离下划线：链接与域名里 `_` 是合法字符，剥离会把 URL 改坏。
    """
    return " ".join(_MD_NOISE.sub("", text or "").split())


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url


def _extract_sources(findings: list[tuple[str, str]], limit: int = 6) -> list[dict]:
    """从工具输出中提取可展示的来源链接（按出现顺序去重，最多 limit 条）。

    只做轻量解析：不假设具体搜索服务（Tavily / Bing / 自建）的返回结构，
    任何形如 markdown 链接或裸 URL 的内容都视为可引用来源。
    两遍扫描：先收带标题的 markdown 链接（信息最全），再用裸链接补漏，
    避免「URL: xxx」这类无标题行抢先占位、丢掉真实标题。
    """
    sources: list[dict] = []
    seen: set[str] = set()

    def add(url: str, title: str, snippet: str) -> None:
        url = url.rstrip(".,;")
        if not url or url in seen:
            return
        seen.add(url)
        sources.append({"title": title or _host(url), "url": url,
                        "snippet": snippet[:200]})

    for _, output in findings:
        lines = [ln.strip() for ln in (output or "").splitlines()]
        for idx, line in enumerate(lines):
            if not line:
                continue
            for m in _MD_LINK.finditer(line):
                title = _clean(m.group(1))
                # 链接独占一行时（搜索结果常见排版），用下一行摘要做片段
                snippet = _clean(_MD_LINK.sub("", line)).lstrip("-—• ")
                if not snippet:
                    snippet = next((_clean(n).lstrip("-—• ") for n in lines[idx + 1:]
                                    if n.strip()), "")
                add(m.group(2), title, snippet or title)

    for _, output in findings:
        for raw in (output or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            snippet = _clean(_MD_LINK.sub(r"\1", line)).lstrip("-—• ")
            for m in _BARE_URL.finditer(line):
                add(m.group(0), "", snippet)
    return sources[:limit]


async def augment(question: str, history: str, *, llm, chunks: list,
                  force: bool = False) -> ToolAugment:
    """补充外部资料：时效性问题直接搜索，其余交给模型自主决定。

    force=True 表示用户在本轮显式开启了「联网搜索」：无论知识库是否召回、
    问题是否带时效性词，都执行一次搜索并回传来源链接。
    """
    mcp = get_mcp()
    if not mcp.enabled or (not force and not should_use_tools(question, chunks)):
        return ToolAugment()
    # 只是问当前日期/时间：system prompt 里的本机时间就是权威答案，无需联网
    if not force and is_clock_question(question):
        return ToolAugment()
    tools = await mcp.list_tools()
    if not tools:
        return ToolAugment()

    findings: list[tuple[str, str]] = []
    # 时效性问题直接调用搜索工具：模型常"自信地"编造日期/版本，等它主动调工具不可靠
    if force or is_time_sensitive(question):
        search = _pick_search_tool(tools)
        args = _direct_args(search, question) if search is not None else None
        if search is not None and args is not None:
            output = await mcp.call(search, args)
            if not _failed(output):
                findings.append((search.qualified, output))
    if not findings:
        findings = await _model_tool_loop(mcp, question, history, llm, tools)

    if not findings:
        return ToolAugment()
    text = "\n\n".join(f"[工具:{name}]\n{out}" for name, out in findings)
    sources = _extract_sources(findings)
    logger.info("mcp_tools_used", tools=[n for n, _ in findings],
                chars=len(text), sources=len(sources), forced=force)
    return ToolAugment(text=text, used=[n for n, _ in findings], sources=sources)


async def _model_tool_loop(mcp, question: str, history: str, llm,
                           tools: list) -> list[tuple[str, str]]:
    """让模型自主决定调用哪些工具（最多 max_rounds 轮）。"""
    specs = [t.to_openai() for t in tools]
    by_name = {t.qualified: t for t in tools}
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",
         "content": question + (f"\n\n# 历史对话\n{history}" if history else "")},
    ]
    findings: list[tuple[str, str]] = []
    try:
        for _ in range(max(1, get_settings().mcp.max_rounds)):
            content, calls = await llm.complete_with_tools(messages, specs, max_tokens=1024)
            if not calls:
                break
            # 只接受已发现工具，过滤模型臆造的函数名
            assistant_calls = [
                {"id": c.get("id") or "", "type": "function",
                 "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"}}
                for c in calls if c.get("name") in by_name
            ]
            if not assistant_calls:
                break
            messages.append({"role": "assistant", "content": content or "",
                             "tool_calls": assistant_calls})
            for call in assistant_calls:
                tool = by_name[call["function"]["name"]]
                output = await mcp.call(tool, _parse_args(call["function"]["arguments"]))
                findings.append((tool.qualified, output))
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": output})
    except Exception as e:                    # 含端点不支持 tools 的能力错误
        logger.warning("mcp_augment_failed", error=str(e))
    return findings


def _parse_args(raw: str) -> dict:
    """工具参数是 JSON 字符串；模型偶尔产出非法 JSON，按空参数执行让服务端报错更明确。"""
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}

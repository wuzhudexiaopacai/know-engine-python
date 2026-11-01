"""查询分析三件套：意图识别 / 查询改写 / 查询路由。

统一降级原则：任一环节 LLM 不可用或输出异常，都回退到"对下游最安全的默认值"，
而不是抛错中断对话（意图→视为相关/其他；改写→原问题；路由→知识库检索）。
"""

from __future__ import annotations

import json

from app.core.logging import get_logger
from app.infra.json_fix import fix_json
from app.modules.rag.model import IntentResult, RouteResult
from app.services.llm_client import get_llm
from app.services.prompt_loader import render

logger = get_logger("app.rag.analysis")


def _parse_json(raw: str) -> dict | None:
    """LLM JSON 输出统一经 7 步修复管道解析，失败返回 None。"""
    if not raw:
        return None
    try:
        return json.loads(fix_json(raw))
    except (json.JSONDecodeError, ValueError):
        return None


async def recognize_intent(question: str, history: str = "",
                           llm=None) -> IntentResult:
    """意图识别：判断相关性 + 分类 + 实体抽取。LLM 不可用时默认"相关+其他"。"""
    llm = llm or get_llm()
    if not llm.available:
        return IntentResult(related=True, intent="其他", reasoning="LLM 未配置，默认按知识库处理")
    try:
        prompt = render("intent-recognition-prompt", question=question)
        if history:
            prompt += f"\n\n# 历史对话\n{history}"
        raw = await llm.chat(prompt, temperature=0.0)
        parsed = _parse_json(raw)
        return IntentResult.from_llm(parsed)
    except Exception as e:
        logger.warning("intent_recognition_failed", error=str(e))
        return IntentResult(related=True, intent="其他", reasoning="意图识别降级")


async def transform_query(question: str, history: str = "", llm=None) -> str:
    """查询改写：为检索提供更精准的查询串。失败返回原问题。"""
    llm = llm or get_llm()
    if not llm.available:
        return question
    try:
        out = await llm.chat(
            render("query-transform-prompt", history=history or "（无）", question=question),
            model=llm.flash_model, temperature=0.0)
        out = (out or "").strip()
        # 防御：改写结果异常（空/过长）时回退原问题
        return out if 0 < len(out) <= 200 else question
    except Exception as e:
        logger.warning("query_transform_failed", error=str(e))
        return question


async def route_query(question: str, table_schemas: str = "",
                      llm=None) -> RouteResult:
    """查询路由：决定 knowledge_base / relational_db / graph_db。失败默认知识库。"""
    llm = llm or get_llm()
    if not llm.available:
        return RouteResult(strategy="knowledge_base", reasoning="LLM 未配置，默认知识库检索", confidence=0.5)
    try:
        raw = await llm.chat(
            render("query-router-prompt", tableSchemas=table_schemas or "（无）", question=question),
            model=llm.flash_model, temperature=0.0)
        parsed = _parse_json(raw) or {}
        strategy = str(parsed.get("strategy", "knowledge_base"))
        if strategy not in ("knowledge_base", "relational_db", "graph_db"):
            strategy = "knowledge_base"
        return RouteResult(strategy=strategy,
                           reasoning=str(parsed.get("reasoning", "")),
                           confidence=float(parsed.get("confidence", 0.0) or 0.0))
    except Exception as e:
        logger.warning("query_route_failed", error=str(e))
        return RouteResult(strategy="knowledge_base", reasoning="路由降级", confidence=0.0)

"""Text2Cypher 图检索器（Neo4j，docker-compose 部署；不可用时降级知识库）。

安全红线同 Text2SQL：
生成的 Cypher 只允许只读 MATCH/RETURN，禁用任何写子句；驱动初始化失败、
连接异常、语句被拒一律回退知识库混合检索，保证对话不因图库缺位而中断。
"""

from __future__ import annotations

import re

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.models.constants import RetrievalSource
from app.modules.rag import retrieval
from app.modules.rag.model import RagReference, RetrievedChunk
from app.services.llm_client import get_llm
from app.services.prompt_loader import render

logger = get_logger("app.rag.graph")

_DRIVER = None          # 惰性单例；初始化异常后置 False 表示"永久不可用"，避免反复重连
_FORBIDDEN = re.compile(
    r"\b(create|merge|delete|remove|set|drop|call\s+db\.|load\s+csv)\b", re.IGNORECASE)


def _get_driver():
    global _DRIVER
    if _DRIVER is False:
        return None
    if _DRIVER is None:
        try:
            from neo4j import AsyncGraphDatabase
            cfg = get_settings().graph
            _DRIVER = AsyncGraphDatabase.driver(
                cfg.uri, auth=(cfg.username, cfg.password),
                max_connection_lifetime=1800, connection_timeout=3)
        except Exception as e:
            logger.warning("neo4j_init_failed", error=str(e))
            _DRIVER = False
            return None
    return _DRIVER


def _is_safe_cypher(cypher: str) -> bool:
    cleaned = cypher.strip().rstrip(";").strip()
    if not cleaned or ";" in cleaned:
        return False
    return bool(re.match(r"^\s*(match|return|with|unwind)\b", cleaned, re.IGNORECASE)) \
        and not _FORBIDDEN.search(cleaned)


async def _run_cypher(cypher: str) -> list[dict] | None:
    driver = _get_driver()
    if driver is None:
        return None
    try:
        cfg = get_settings().graph
        async with driver.session(database=cfg.database) as session:
            result = await session.run(cypher)
            return [record.data() async for record in result][:50]
    except Exception as e:
        logger.warning("cypher_exec_failed", error=str(e), cypher=cypher[:200])
        return None


async def query_graph(question: str, *, user_identity: str | None = None,
                      doc_versions: list[int] | None = None) -> tuple[list[RetrievedChunk], list[RagReference], dict | None]:
    """图关系查询；Neo4j 不可用/语句非法/执行失败 → 降级知识库检索（card=None）。

    降级检索必须带上与知识库路由一致的可见性（user_identity）与生效版本
    （doc_versions），否则会绕过文档权限过滤。
    """
    llm = get_llm()
    fallback = await retrieval.retrieve_and_aggregate(
        question, user_identity=user_identity, doc_versions=doc_versions)
    if not llm.available:
        return fallback[0], fallback[1], None

    try:
        raw = await llm.chat(
            render("text-to-cypher-prompt", question=question), temperature=0.0)
    except Exception:
        return fallback[0], fallback[1], None

    cypher = _extract_cypher(raw or "")
    if not cypher:       # 提示词约定：Schema 覆盖不到时模型返回空串，属正常不走图库
        logger.info("cypher_not_applicable", question=question[:80])
        return fallback[0], fallback[1], None
    if not _is_safe_cypher(cypher):
        logger.warning("cypher_rejected", cypher=cypher[:200])
        return fallback[0], fallback[1], None

    rows = await _run_cypher(cypher)
    if rows is None:      # Neo4j 不可达 → 降级
        return fallback[0], fallback[1], None

    if not rows:
        text_out = "图数据库查询完成，未找到匹配的关系数据。"
    else:
        text_out = "图数据库查询结果：\n" + "\n".join(str(r) for r in rows)
    chunk = RetrievedChunk(chunk_id="", text=text_out, score=1.0, document_id=0,
                           source=RetrievalSource.GRAPH.value, metadata={"cypher": cypher})
    ref = RagReference(document_id="0", document_title="知识图谱查询", chunk_id=None,
                       version=None, chunk_content=text_out[:500], similarity_score=1.0,
                       retrieval_source=RetrievalSource.GRAPH.value)
    card = {"type": "comparison_table", "title": "图谱关系查询结果",
            "columns": list(rows[0].keys()) if rows else [],
            "data": rows} if rows else None
    return [chunk], [ref], card


def _extract_cypher(raw: str) -> str:
    raw = raw.strip()
    fence = re.search(r"```(?:cypher)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
    return fence.group(1).strip() if fence else raw

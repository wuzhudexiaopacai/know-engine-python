"""Text2SQL 检索器：自然语言 → SELECT → 结构化结果。

安全红线：
- LLM 产出的 SQL 必须通过白盒校验（仅单条 SELECT/WITH，禁 DML/DDL/分号拼接），
  校验不通过直接拒绝执行并降级知识库检索 —— 绝不把未校验 SQL 打到数据库。
- 结果行数硬上限，防大结果集撑爆 Prompt。
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.constants import RetrievalSource
from app.models.entities import TableMeta
from app.modules.rag.model import RagReference, RetrievedChunk
from app.modules.rag import retrieval
from app.services.llm_client import get_llm
from app.services.prompt_loader import render

logger = get_logger("app.rag.text2sql")

# 危险关键字：出现即判为非只读语句
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|"
    r"attach|detach|pragma|exec|execute)\b", re.IGNORECASE)
_MAX_ROWS = 50


def _is_safe_select(sql: str) -> bool:
    """只允许单条 SELECT / WITH 查询：去空白后必须以 select/with 开头，
    无多语句分号（允许结尾单分号），且不含任何禁用关键字。"""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned or ";" in cleaned:
        return False
    if not re.match(r"^\s*(select|with)\b", cleaned, re.IGNORECASE):
        return False
    return not _FORBIDDEN.search(cleaned)


async def _database_structure() -> str:
    """拼接 Text2SQL 的 Schema 感知：静态表（table_meta 种子）+ 动态建表。"""
    async with session_scope() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(TableMeta.create_sql).where(TableMeta.deleted == 0))).scalars().all()
    return "\n\n".join(r for r in rows if r)


def _format_rows(columns: list[str], rows: list[tuple]) -> str:
    """结果集 → Markdown 表格（LLM 友好且前端可直接渲染）。"""
    if not rows:
        return "（查询成功，无匹配数据）"
    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return "\n".join([head, sep, body])


async def query_structured(question: str, *, user_identity: str | None = None,
                           doc_versions: list[int] | None = None) -> tuple[list[RetrievedChunk], list[RagReference], dict | None]:
    """执行结构化查询；任何环节失败都降级知识库混合检索。

    降级检索带 user_identity / doc_versions，保证与知识库路由一致的文档可见性。
    返回 (chunks, references, card)，card 为结构化卡片数据（供前端表格展示），
    降级走知识库时 card=None。
    """
    async def _fallback():
        return await retrieval.retrieve_and_aggregate(
            question, user_identity=user_identity, doc_versions=doc_versions)

    llm = get_llm()
    if not llm.available:
        chunks, refs = await _fallback()
        return chunks, refs, None

    schema = await _database_structure()
    try:
        sql_raw = await llm.chat(
            render("text-to-sql-prompt", databaseStructure=schema,
                   today=date.today().isoformat(), question=question),
            temperature=0.0)
    except Exception as e:  # LLM 不可用 → 降级
        logger.warning("text2sql_llm_failed", error=str(e))
        chunks, refs = await _fallback()
        return chunks, refs, None

    sql = _extract_sql(sql_raw or "")
    if not sql or not _is_safe_select(sql):
        logger.warning("text2sql_rejected", sql=(sql or "")[:200])
        chunks, refs = await _fallback()
        return chunks, refs, None

    try:
        async with session_scope() as session:
            result = await session.execute(text(sql))
            columns = list(result.keys())
            rows = result.fetchmany(_MAX_ROWS)
    except SQLAlchemyError as e:
        logger.warning("text2sql_exec_failed", error=str(e), sql=sql[:200])
        chunks, refs = await _fallback()
        return chunks, refs, None

    formatted = _format_rows(columns, rows)
    chunk = RetrievedChunk(
        chunk_id="", text=f"根据问题执行的 SQL 查询结果：\n{formatted}",
        score=1.0, document_id=0, source=RetrievalSource.SQL.value,
        metadata={"sql": sql})
    ref = RagReference(
        document_id="0", document_title="结构化数据查询", chunk_id=None,
        version=None, chunk_content=formatted[:500], similarity_score=1.0,
        retrieval_source=RetrievalSource.SQL.value)
    card = {"type": "comparison_table", "title": "结构化查询结果",
            "columns": columns, "data": [dict(zip(columns, r)) for r in rows]}
    return [chunk], [ref], card


def _extract_sql(raw: str) -> str:
    """从 LLM 返回中剥离 markdown 代码块围栏，取出纯 SQL。"""
    raw = raw.strip()
    fence = re.search(r"```(?:sql)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    return raw

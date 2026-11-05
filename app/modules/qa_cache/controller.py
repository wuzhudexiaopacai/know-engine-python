"""问答缓存审核 REST 接口（均限管理员/审核员）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, require_permission
from app.core.response import R
from app.modules.qa_cache import service as qa_cache
from app.modules.rbac.constants import PermCode

router = APIRouter(prefix="/api/qa-cache", tags=["问答缓存"])


class ReviewAction(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.get("/entries")
async def list_entries(status: str | None = None, page: int = Query(1, ge=1),
                       size: int = Query(20, ge=1, le=100),
                       user: CurrentUser = Depends(require_permission(PermCode.QA_REVIEW))) -> R:
    return R.ok(await qa_cache.list_entries(status, page, size))


@router.post("/entries/{entry_id}/approve")
async def approve(entry_id: str, body: ReviewAction,
                  user: CurrentUser = Depends(require_permission(PermCode.QA_REVIEW))) -> R:
    result = await qa_cache.approve(entry_id, user.name or user.sub, body.note)
    # 索引失败时会停留在 APPROVED，由补偿任务补齐后自动上线
    message = "已通过并上线" if result.get("status") == "ACTIVE" else "已通过审核，索引建立中，稍后自动上线"
    return R.ok(result, message)


@router.post("/entries/{entry_id}/reject")
async def reject(entry_id: str, body: ReviewAction,
                 user: CurrentUser = Depends(require_permission(PermCode.QA_REVIEW))) -> R:
    return R.ok(await qa_cache.reject(entry_id, user.name or user.sub, body.note), "已驳回")


@router.post("/entries/{entry_id}/disable")
async def disable(entry_id: str, reason: str = "MANUAL", detail: str | None = None,
                  user: CurrentUser = Depends(require_permission(PermCode.QA_REVIEW))) -> R:
    return R.ok(await qa_cache.disable(entry_id, reason, detail), "已下线")

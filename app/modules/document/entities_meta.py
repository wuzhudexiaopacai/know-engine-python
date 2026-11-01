"""文档版本工具。"""

from __future__ import annotations


def next_version_from(versions: list[str | None]) -> str:
    """在所有历史版本号中取最大值并把补丁位 +1。

    为什么不能读 doc.extension.version：
    - 该字段会在流水线异常/重试时被覆盖或缺失；
    - 版本行采用逻辑删除，已删除版本仍占用 uk_doc_version 唯一键，
      必须以"全量历史版本号"为基准递增，否则会撞唯一索引。
    """
    best = (0, 0, 0)
    for raw in versions:
        parts = (raw or "").strip().split(".")
        if len(parts) != 3:
            continue
        try:
            parsed = tuple(int(x) for x in parts)
        except ValueError:
            continue
        best = max(best, parsed)
    return f"{best[0]}.{best[1]}.{best[2] + 1}"

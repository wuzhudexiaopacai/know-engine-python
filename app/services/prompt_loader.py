"""Prompt 模板加载器。

- 文件位于 app/prompts/*.txt，{{var}} 占位符渲染
- 进程内缓存，避免重复 IO
- 渲染缺失变量时抛 KeyError 快速暴露，而不是静默输出残缺 Prompt
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.exceptions import BizError

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_cache: dict[str, str] = {}

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def load_prompt(name: str) -> str:
    """按文件名（不含后缀）读取模板并缓存。"""
    if name not in _cache:
        path = _PROMPT_DIR / f"{name}.txt"
        if not path.exists():
            raise BizError(f"Prompt 模板不存在: {name}")
        _cache[name] = path.read_text(encoding="utf-8")
    return _cache[name]


def render(name: str, **variables) -> str:
    """渲染 {{var}} 占位符。

    上下文注入前统一做长度截断，防 Prompt 膨胀（LLM 输入成本与注入攻击面控制）。
    """
    template = load_prompt(name)
    missing = {v for v in _VAR_RE.findall(template) if v not in variables}
    if missing:
        raise BizError(f"Prompt [{name}] 缺少变量: {missing}")
    rendered = template
    for key, value in variables.items():
        text = str(value) if value is not None else ""
        rendered = rendered.replace("{{" + key + "}}", text[:30_000])
    return rendered

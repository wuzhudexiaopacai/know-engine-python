"""LLM JSON 输出修复工具（7 步容错管道）。

LLM 在结构化输出场景中经常产生非法 JSON：包裹 Markdown 代码块、
中文引号、尾部逗号、键名缺引号、字符串内裸换行等。
修复后统一经 json.loads 验证，确保下游永远拿到合法 JSON ——
意图识别、查询路由、查询改写全部依赖此工具。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 各步修复的正则（预编译，避免热路径重复编译）
_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_UNQUOTED_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)\s*:")
_SINGLE_QUOTE_RE = re.compile(r"'")


def fix_json(raw: str | None) -> str:
    """执行 7 步修复管道，返回合法 JSON 字符串（永不抛异常）。"""
    if raw is None or not raw.strip():
        return '{"content": ""}'

    text = raw.strip()

    # 1. Markdown 代码块提取：剥离 ```json ... ``` 包裹
    m = _CODE_BLOCK_RE.search(text)
    if m:
        text = m.group(1)

    # 2. 前后垃圾字符移除：定位首个 { 或 [ 到末尾 } 或 ]
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    end = max((i for i in (text.rfind("}"), text.rfind("]")) if i != -1), default=-1)
    if start != -1 and end > start:
        text = text[start:end + 1]

    # 3. 引号修复：中文引号 → 英文引号（单引号谨慎处理，仅在非字符串值场景）
    text = text.replace("\u201c", '"').replace("\u201d", '"') \
               .replace("\u2018", "'").replace("\u2019", "'")

    # 4. 尾部逗号修复：移除 },  ],  中的多余逗号
    text = _TRAILING_COMMA_RE.sub(r"\1", text)

    # 5. 缺失引号修复：为无引号的键名补双引号
    text = _UNQUOTED_KEY_RE.sub(r'\1"\2":', text)

    # 6. 转义字符清理：字符串内的裸换行/制表符（整体替换，容忍少量过度转义）
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\t", "\\t")

    # 7. 最终容错：仍非法则包装为 {"content": ...}
    if not _is_valid(text):
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
        text = '{"content": "%s"}' % re.sub(r"[\r\n\t]", " ", escaped)

    return text


def _is_valid(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def parse_json(raw: str | None, default: Any = None) -> Any:
    """修复 + 解析一步到位；解析失败返回 default（调用方决定是否降级）。"""
    try:
        return json.loads(fix_json(raw))
    except (json.JSONDecodeError, ValueError):
        return default


def to_camel(text: str) -> str:
    """snake_case → camelCase（LLM prompt 中字段名与 Python 属性名桥接）。"""
    parts = text.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])

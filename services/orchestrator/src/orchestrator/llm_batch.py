"""LLM 长上下文分批：按自然边界切输入，禁止切断 JSON 实体。

借鉴 htftest-agent 的章节/段落优先切分策略。分批只控制单次模型输入与
输出规模；跨批聚合、ID 重编号和 Schema 校验仍由具体节点负责。
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

T = TypeVar("T")

# 中文文档按字符切分更稳定；约 3,000 个中文字符可为 JSON 输出留出足够余量。
DEFAULT_TEXT_CHUNK_CHARS = 3_000
DEFAULT_JSON_BATCH_CHARS = 12_000
_HEADING_RE = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)


def chunk_text(text: str, max_chars: int = DEFAULT_TEXT_CHUNK_CHARS) -> list[str]:
    """按标题、段落、行依次切分；每块保留最近章节标题作为上下文。"""
    if len(text) <= max_chars:
        return [text]

    units = _split_text_units(text)
    chunks: list[str] = []
    current = ""
    heading = ""
    for unit in units:
        if _HEADING_RE.match(unit.strip()):
            heading = unit.strip()
        prefix = f"{heading}\n\n" if heading and not current else ""
        if current and len(current) + len(unit) + 2 > max_chars:
            chunks.append(current)
            current = prefix
        if len(unit) > max_chars:
            for offset in range(0, len(unit), max_chars):
                piece = unit[offset : offset + max_chars]
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(f"{heading}\n\n{piece}" if heading and offset else piece)
            continue
        current = f"{current}\n\n{unit}" if current else f"{prefix}{unit}"
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def chunk_json_items(items: list[T], max_chars: int = DEFAULT_JSON_BATCH_CHARS) -> list[list[T]]:
    """按序列化字符预算分批，单个大实体独占一批，保持输入顺序。"""
    batches: list[list[T]] = []
    current: list[T] = []
    current_size = 2
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1
        if current and current_size + item_size > max_chars:
            batches.append(current)
            current = []
            current_size = 2
        current.append(item)
        current_size += item_size
    if current:
        batches.append(current)
    return batches


def _split_text_units(text: str) -> list[str]:
    """标题保留为独立单元，其余优先按空行切段。"""
    pieces = _HEADING_RE.split(text)
    units: list[str] = []
    for piece in pieces:
        if not piece.strip():
            continue
        if _HEADING_RE.match(piece.strip()):
            units.append(piece.strip())
        else:
            units.extend(part.strip() for part in piece.split("\n\n") if part.strip())
    return units

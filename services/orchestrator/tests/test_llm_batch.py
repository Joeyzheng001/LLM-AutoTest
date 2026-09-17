"""长上下文分批：自然边界优先、JSON 实体不被拆开。"""

from __future__ import annotations

from orchestrator.llm_batch import chunk_json_items, chunk_text


def test_chunk_text_keeps_heading_context() -> None:
    """大需求按段落切分，后续批次仍携带最近章节标题。"""
    text = "# 用户管理\n\n" + "A" * 20 + "\n\n" + "B" * 20 + "\n\n## 删除用户\n\n" + "C" * 20
    chunks = chunk_text(text, max_chars=30)
    assert len(chunks) >= 3
    assert any("# 用户管理" in chunk for chunk in chunks)
    assert any("## 删除用户" in chunk for chunk in chunks)


def test_chunk_json_items_never_splits_item() -> None:
    """候选/接口等 JSON 实体只能整体进入一批。"""
    items = [{"id": "A", "body": "x" * 20}, {"id": "B", "body": "y" * 20}]
    batches = chunk_json_items(items, max_chars=30)
    assert batches == [[items[0]], [items[1]]]

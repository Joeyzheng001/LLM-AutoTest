"""F-GEN-05 展开充分性校验：_underexpanded_testpoints 纯函数测试。

覆盖面与 _uncovered_req_testpoints 对齐——只校验 source=REQ 且 hints 维度足够多
的测试点；RISK 点、hints 过少的点、未被引用的点均豁免（分别归 prompt 聚合、
F-GEN-04、噪声控制处理）。
"""

from __future__ import annotations

from executor.nodes.testcase_generate import (
    _EXPANSION_MIN_HINTS,
    _underexpanded_testpoints,
)


def _point(point_id: str, source: str, hint_count: int) -> dict:
    """造一个带 hint_count 条拆分维度的测试点。"""
    return {
        "id": point_id,
        "source": source,
        "case_split_hints": [f"维度{i}" for i in range(hint_count)],
    }


def _cases(testpoint_id: str, count: int) -> list[dict]:
    """造 count 条引用同一测试点的用例。"""
    return [{"testpoint_id": testpoint_id, "category": "normal"} for _ in range(count)]


def test_hints_many_but_cases_few_flagged() -> None:
    """5 维度只产 1 条 → 期望 ≥3，实际 1，应告警。"""
    points = [_point("TP-1", "REQ", 5)]
    result = _underexpanded_testpoints(_cases("TP-1", 1), points)
    assert len(result) == 1
    tid, hint_count, expected, actual = result[0]
    assert tid == "TP-1"
    assert hint_count == 5
    assert expected == 3  # ceil(5 * 0.6)
    assert actual == 1


def test_expanded_per_dimension_not_flagged() -> None:
    """5 维度产 4 条 → 达到期望，不告警。"""
    points = [_point("TP-1", "REQ", 5)]
    result = _underexpanded_testpoints(_cases("TP-1", 4), points)
    assert result == []


def test_exactly_expected_not_flagged() -> None:
    """实际恰等于期望（边界）不告警：ceil(4*0.6)=3，产 3 条。"""
    points = [_point("TP-1", "REQ", 4)]
    result = _underexpanded_testpoints(_cases("TP-1", 3), points)
    assert result == []


def test_risk_point_exempt() -> None:
    """RISK 点维度再多也不校验（按需求聚合，逐点校验会逼出雷同用例）。"""
    points = [_point("TP-1", "RISK", 6)]
    result = _underexpanded_testpoints(_cases("TP-1", 1), points)
    assert result == []


def test_few_hints_exempt() -> None:
    """hints 低于下限（2 条）不校验，交 F-GEN-04 的「≥1」兜底。"""
    assert _EXPANSION_MIN_HINTS == 3
    points = [_point("TP-1", "REQ", 2)]
    result = _underexpanded_testpoints(_cases("TP-1", 1), points)
    assert result == []


def test_zero_cases_exempt() -> None:
    """完全未被引用的点归 F-GEN-04，本函数不重复告警。"""
    points = [_point("TP-1", "REQ", 5)]
    result = _underexpanded_testpoints([], points)
    assert result == []


def test_missing_hints_key_exempt() -> None:
    """没有 case_split_hints 键（视为 0 维度）不告警。"""
    points = [{"id": "TP-1", "source": "REQ"}]
    result = _underexpanded_testpoints(_cases("TP-1", 1), points)
    assert result == []


def test_hints_not_list_exempt() -> None:
    """case_split_hints 非列表（脏数据）按 0 维度处理，不崩溃不告警。"""
    points = [{"id": "TP-1", "source": "REQ", "case_split_hints": "单条字符串"}]
    result = _underexpanded_testpoints(_cases("TP-1", 1), points)
    assert result == []


def test_mixed_points_only_flags_underexpanded() -> None:
    """多点混合：仅展开不足的 REQ 点进结果，其余各按豁免规则放行。"""
    points = [
        _point("TP-under", "REQ", 6),   # 产 1 条 → 期望 4，告警
        _point("TP-ok", "REQ", 5),      # 产 5 条 → 不告警
        _point("TP-risk", "RISK", 7),   # RISK → 豁免
        _point("TP-few", "REQ", 2),     # 维度少 → 豁免
    ]
    cases = (
        _cases("TP-under", 1)
        + _cases("TP-ok", 5)
        + _cases("TP-risk", 1)
        + _cases("TP-few", 1)
    )
    result = _underexpanded_testpoints(cases, points)
    assert [row[0] for row in result] == ["TP-under"]

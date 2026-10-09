"""response_assertions 测试：路径语法校验与解析的一致性（回归 X-LLM-02 误报）。

背景：校验用的 ``_PATH_RE`` 一度要求每个后续段以字面量 ``.`` 开头，导致标准写法
``data.items[0].id``（下标紧跟标识符）被判为非法路径，而 ``_resolve_json_path``
明明能正确解析它。用例生成因此误报 X-LLM-02 并降级（连带下游 4 节点缺前置产物）。
本文件锁定「校验接受 ⇔ 解析可得值」这一不变式。
"""

from __future__ import annotations

import pytest

from common.response_assertions import (
    evaluate_response_assertions,
    legacy_response_assertions,
    normalize_response_assertions,
)

# 合法路径：均应按 prompt 约定（「点路径，可带数组下标」）被接受
_VALID_PATHS = (
    "data",
    "a.b.c",
    "snake_case_key",
    "key-with-dash",  # JSON 键名常含连字符
    "data[0]",  # 下标紧跟标识符
    "a[0]",
    "[0]",  # 纯下标（根为数组）
    "[0].a",
    "a.b[0]",
    "a[0].b",
    "data.items[0].id",  # 本次事故的形态
    "data.list[2].status",
    "data.result[0].code",
)

# 非法路径：语法残缺或含不支持的字符
_INVALID_PATHS = (
    "",
    "a.",
    ".a",
    "a..b",
    "a[",
    "a[]",
    "a[0",
    "1abc",  # 数字开头的标识符
    "a b",  # 含空格
    "a[0]b",  # 下标后直接跟标识符，缺分隔符
    "a[b]",  # 下标非数字
)


@pytest.mark.parametrize("path", _VALID_PATHS)
def test_valid_paths_are_accepted(path: str) -> None:
    """标准写法必须被接受（含下标紧跟标识符的形态）。"""
    normalized = normalize_response_assertions([{"path": path, "op": "exists"}])
    assert normalized == [{"path": path, "op": "exists"}]


@pytest.mark.parametrize("path", _INVALID_PATHS)
def test_invalid_paths_are_rejected(path: str) -> None:
    """语法残缺或含不支持字符的路径必须拒绝。"""
    with pytest.raises(ValueError, match="不是受支持的 JSON 路径"):
        normalize_response_assertions([{"path": path, "op": "exists"}])


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        ("data.items[0].id", {"data": {"items": [{"id": 7}]}}, 7),
        ("data[0].name", {"data": [{"name": "x"}]}, "x"),
        ("a[0]", {"a": ["first"]}, "first"),
        ("[0].a", [{"a": 1}], 1),
        ("a.b[0]", {"a": {"b": [True]}}, True),
        ("a.b.c", {"a": {"b": {"c": "deep"}}}, "deep"),
    ],
)
def test_validated_paths_are_actually_resolvable(path: str, body: object, expected: object) -> None:
    """不变式：凡校验通过的路径，求值都能取到值（校验不得比解析更严格）。

    这正是本次事故的根因——校验拒绝了解析器本可处理的路径。
    """
    assertions = normalize_response_assertions([{"path": path, "op": "exists"}])
    outcomes = evaluate_response_assertions(body, assertions)
    assert outcomes[0]["found"] is True
    assert outcomes[0]["actual"] == expected


def test_dollar_prefix_is_stripped() -> None:
    """``$.`` / ``$`` 前缀（JSONPath 习惯写法）被剥掉后仍合法。"""
    assert normalize_response_assertions([{"path": "$.data.id", "op": "exists"}]) == [
        {"path": "data.id", "op": "exists"}
    ]
    assert normalize_response_assertions([{"path": "$[0].id", "op": "exists"}]) == [
        {"path": "[0].id", "op": "exists"}
    ]


def test_missing_value_for_value_operators_is_rejected() -> None:
    """需要 value 的算子缺 value 必须拒绝（否则断言无法判定）。"""
    with pytest.raises(ValueError, match="缺少 value"):
        normalize_response_assertions([{"path": "code", "op": "equals"}])
    # exists 类算子不需要 value
    assert normalize_response_assertions([{"path": "code", "op": "exists"}])


def test_unknown_operator_is_rejected() -> None:
    """表外算子必须拒绝，不静默当成默认行为。"""
    with pytest.raises(ValueError, match="op 不支持"):
        normalize_response_assertions([{"path": "code", "op": "matches"}])


def test_evaluate_handles_array_index_out_of_range() -> None:
    """越界下标求值为「未找到」，不抛异常。"""
    assertions = normalize_response_assertions([{"path": "items[5].id", "op": "exists"}])
    outcomes = evaluate_response_assertions({"items": [{"id": 1}]}, assertions)
    assert outcomes[0]["found"] is False
    assert outcomes[0]["passed"] is False


def test_legacy_syntax_skips_array_paths_when_malformed() -> None:
    """遗留 ``field=value`` 语法：非法路径被跳过，合法路径转成断言。"""
    assertions = legacy_response_assertions("data.id=42; 1bad=x; code=ok")
    assert {a["path"] for a in assertions} == {"data.id", "code"}

"""common.paths 测试：路径口径归一与跨分隔符比较。

背景：仓库在 Windows 上开发，``str(Path)`` 产出反斜杠，而 git/java-parser 产出
正斜杠；两者混用会让字符串比较永不命中（X-REV-04）。本模块是唯一归一入口。
"""

from __future__ import annotations

from pathlib import Path

from common.paths import normalize_path, paths_equal


def test_normalize_converts_backslashes_to_posix() -> None:
    """反斜杠（Windows str(Path) 的产出）归一为正斜杠。"""
    assert normalize_path(r"E:\code\a\b\c.docx") == "E:/code/a/b/c.docx"


def test_normalize_is_idempotent() -> None:
    """已归一的输入重复归一不变——调用方不必关心是否已处理过。"""
    once = normalize_path(r"E:\code\a\b.txt")
    assert normalize_path(once) == once
    assert normalize_path(Path(once)) == once


def test_normalize_accepts_path_objects() -> None:
    """Path 与 str 接受同一种口径。"""
    assert normalize_path(Path("a/b/c.py")) == "a/b/c.py"


def test_normalize_empty_values_return_empty_string() -> None:
    """None/空串/纯空白返回空串——避免 Path('') 退化成 '.' 这类静默错误。"""
    assert normalize_path(None) == ""
    assert normalize_path("") == ""
    assert normalize_path("   ") == ""
    # 关键：空值不得退化为当前目录
    assert normalize_path("") != "."


def test_normalize_keeps_absolute_and_relative_shapes() -> None:
    """绝对路径保持绝对（盘符为大写、无前导斜杠丢失），相对路径保持相对。"""
    assert normalize_path(r"E:\code\x") == "E:/code/x"
    assert normalize_path(r"src\main\java") == "src/main/java"
    assert not normalize_path("src/main/java").startswith("/")


def test_paths_equal_ignores_separator_style() -> None:
    """比较跨风格：历史反斜杠数据与新正斜杠数据视为同一路径。"""
    assert paths_equal(r"E:\code\a\b", "E:/code/a/b")
    assert paths_equal("E:/code/a/b", Path(r"E:\code\a\b"))


def test_paths_equal_ignores_trailing_separator() -> None:
    """尾随分隔符不影响判定（目录路径的常见书写差异）。"""
    assert paths_equal("E:/code/a/b/", "E:/code/a/b")
    assert paths_equal(r"E:\code\a\b\\", "E:/code/a/b")


def test_paths_equal_rejects_different_paths() -> None:
    """确实不同的路径不得被判为相等（避免归一过度导致误放行）。"""
    assert not paths_equal("E:/code/a/b", "E:/code/a/c")
    assert not paths_equal("E:/code/ab", "E:/code/a/b")


def test_paths_equal_handles_empty_consistently() -> None:
    """空值互等，但空值与实际路径不等。"""
    assert paths_equal(None, None)
    assert paths_equal("", "")
    assert paths_equal(None, "")
    assert not paths_equal("", "E:/code/a")
    assert not paths_equal(None, "E:/code/a")

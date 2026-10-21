"""路径口径统一：持久化一律正斜杠，跨平台稳定。

背景：本项目在 Windows 上开发，``str(Path(...))`` 产出反斜杠，而 git 的
``--name-status`` 与 java-parser 的产出是正斜杠。两者做字符串比较永不相等——
已有先例修复（X-REV-04：候选被变更范围全量过滤）。此处把口径收敛到
「**存储即正斜杠**」，与 git 和 java-parser 的 ``normalizeRelative`` 对齐。

**兼容性**：``Path(值)`` 在 Windows 上同时接受 ``/`` 与 ``\\``，在 Linux 上
``\\`` 虽属合法文件名字符但不会出现在这些路径中，故读取历史任务（旧的反斜杠
数据）无需任何迁移；只有**字符串比较**处需要经 :func:`normalize_path` 归一后再比。
"""

from __future__ import annotations

from pathlib import Path


def normalize_path(value: str | Path | None) -> str:
    """把路径归一为存储/比较口径（正斜杠）。

    幂等：已是正斜杠的输入原样返回。``None`` 与空串返回空串（调用方据此判空，
    避免 ``Path("")`` 退化成 ``"."`` 这类静默错误）。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # POSIX 的 Path 不把反斜杠视为分隔符；先归一历史 Windows 产物。
    return Path(text.replace("\\", "/")).as_posix()


def paths_equal(left: str | Path | None, right: str | Path | None) -> bool:
    """比较两个路径是否指向同一位置，忽略分隔符风格与尾随分隔符。

    供字符串比较点使用：两侧可能是历史反斜杠数据与新正斜杠数据的组合。
    """
    normalized_left = normalize_path(left)
    normalized_right = normalize_path(right)
    if not normalized_left or not normalized_right:
        return normalized_left == normalized_right
    return normalized_left.rstrip("/") == normalized_right.rstrip("/")

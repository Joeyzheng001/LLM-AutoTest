"""从源码字段说明提取有据可查的编码值，不推断未写明的业务规则。"""

from __future__ import annotations

import re

_CODE_RE = re.compile(r"(?<![A-Za-z0-9-])(\d{2})(?![\d-])")
_SINGLE_CODE_CONTEXT = ("类型", "状态", "编码", "方式", "标志", "结果")


def documented_codes(description: str) -> tuple[dict[str, str], list[str], str]:
    """返回展示名→编码、源码列出的编码、证据类型。

    ``如 00、01`` 只是示例，不能宣称已枚举全部合法值。
    """
    matches = list(_CODE_RE.finditer(description))
    labels: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(description)
        label = description[match.end() : end].strip(" \t\n，,；;、:：")
        if label and not label.isdigit():
            labels[label] = match.group(1)
            if "（" in label and any(hint in label for hint in ("仅", "必填", "适用")):
                labels[label.split("（", 1)[0].strip()] = match.group(1)
            if "，" in label and "精确匹配" in label:
                labels[label.split("，", 1)[0].strip()] = match.group(1)
    if labels and (
        len(matches) >= 2 or any(hint in description for hint in _SINGLE_CODE_CONTEXT)
    ):
        return labels, list(dict.fromkeys(labels.values())), "coded_values"
    if "如" in description and len(matches) >= 2:
        return {}, list(dict.fromkeys(match.group(1) for match in matches)), "examples_only"
    return {}, [], "unspecified"

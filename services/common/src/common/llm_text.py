"""LLM 输出文本的 JSON 提取工具：剥代码围栏、从混杂文本提取完整 JSON 对象。

由 orchestrator（req_parse / interface_reconstruct / code_review）与
test-executor（testcase_generate）共同使用，故下沉到 common 包。
"""

from __future__ import annotations

import json


def strip_code_fence(text: str) -> str:
    """剥掉围栏，并从模型附带的思考/解释中提取一个完整 JSON 对象。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        # 去首行围栏（``` 或 ```json）
        first_newline = stripped.find("\n")
        if first_newline == -1:
            return stripped.removeprefix("```").strip()
        body = stripped[first_newline + 1 :]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        stripped = body.strip()
    return extract_complete_json_object(stripped)


def extract_complete_json_object(text: str) -> str:
    """返回文本中的首个完整 JSON 对象；找不到时保留原文交由 json.loads 报错。

    仅接受花括号、字符串转义和嵌套结构都闭合的对象，不拼接或猜测残缺 JSON。
    整体就是完整 JSON 数组时原样返回（裸数组是已登记容器形态，
    截取首个元素会丢失其余条目）。
    """
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, list):
            return stripped
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        quoted = False
        escaped = False
        for end in range(start, len(text)):
            current = text[end]
            if quoted:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    quoted = False
                continue
            if current == '"':
                quoted = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : end + 1]
                    try:
                        data = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(data, dict):
                        return candidate
                    break
    return text

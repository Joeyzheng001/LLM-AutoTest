"""Bind equivalent Java citations back to an exact original source span.

Only lexical whitespace/comments and adjacent constant string concatenation are
normalized. Identifiers, operators, literal contents and their order stay intact.
"""
from __future__ import annotations

import json
import re
from typing import Any

_TOKENS = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/'
    r'|>>>=|>>=|<<=|>>>|>>|<<|\.\.\.|::|->|\+\+|--|==|!=|<=|>=|&&|\|\|'
    r'|\+=|-=|\*=|/=|%=|&=|\|=|\^=|[\w$]+|[^\s]',
)


def _tokens(source: str) -> list[tuple[str, str, int, int]]:
    result: list[tuple[str, str, int, int]] = []
    for match in _TOKENS.finditer(source):
        raw = match.group()
        if raw.startswith(('//', '/*')):
            continue
        kind, value = 'token', raw
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
                kind = 'string'
            except ValueError:
                pass  # Unsupported Java escapes must remain byte-exact.
        token = (kind, value, match.start(), match.end())
        if (kind == 'string' and len(result) >= 2
                and result[-1][0:2] == ('token', '+') and result[-2][0] == 'string'):
            left = result[-2]
            result[-2:] = [('string', left[1] + value, left[2], match.end())]
        else:
            result.append(token)
    return result


def exact_java_source_quote(quote: str, source: str) -> str | None:
    if not quote:
        return None
    if quote in source:
        return quote
    wanted = _tokens(quote)
    actual = _tokens(source)
    signature = [(kind, value) for kind, value, _, _ in wanted]
    if not signature:
        return None
    matches: set[str] = set()
    for index in range(len(actual) - len(wanted) + 1):
        window = actual[index:index + len(wanted)]
        if [(kind, value) for kind, value, _, _ in window] == signature:
            matches.add(source[window[0][2]:window[-1][3]])
    return next(iter(matches)) if len(matches) == 1 else None


def factory_assertion_issue(oracle: dict[str, Any], texts: dict[str, str]) -> str | None:
    """A literal from a factory proves a path only if that path calls the factory."""
    from common.java_response_paths import _close, _mask
    branch = re.fullmatch(r'(EVD-\d+):(if|catch|return):(\d+)', str(oracle.get('branch_id', '')))
    if branch is None:
        return None
    ref, kind, number = branch.groups()
    source = texts.get(ref, '')
    code = _mask(source)
    guards = list(re.finditer(r'\b' + kind + (r'\s*\(' if kind != 'return' else r'\b[^;{}]*;'), code))
    if not number.isdigit() or not 0 < int(number) <= len(guards):
        return None
    guard = guards[int(number)-1]
    if kind == 'return':
        arm = source[guard.start():guard.end()]
    else:
        opening = code.find('(', guard.start(), guard.end())
        end = _close(code, opening, '(', ')')
        if end is None:
            return None
        brace = end+1
        while brace < len(code) and code[brace].isspace():
            brace += 1
        if brace >= len(code) or code[brace] != '{':
            return None
        close = _close(code, brace, '{', '}')
        if close is None:
            return None
        arm = source[brace:close+1] if oracle.get('branch_outcome', True) else source[close+1:]
    for index, assertion in enumerate(oracle.get('assertions', [])):
        if assertion.get('op') != 'equals' or assertion.get('value') is None:
            continue
        for quote in oracle.get('evidence_quotes', []):
            if quote.get('target') != f'assertions[{index}]' or not re.search(r'\.set\w+\s*\(', str(quote.get('quote', ''))):
                continue
            cited = texts.get(str(quote.get('evidence_ref', '')), '')
            factory = re.search(r'\bpublic\s+static\s+(\w+)\s+(\w+)\s*\(\s*\)\s*\{', _mask(cited))
            if not factory or not re.search(r'\bnew\s+'+re.escape(factory.group(1))+r'\s*\(', _mask(cited)):
                continue
            factory_end = _close(_mask(cited), factory.end()-1, '{', '}')
            actual_quote = str(quote.get('quote', ''))
            if factory_end is None or actual_quote not in cited[factory.start():factory_end+1]:
                continue
            if not re.search(r'\b'+re.escape(factory.group(1))+r'\s*\.\s*'+re.escape(factory.group(2))+r'\s*\(', _mask(arm)):
                return (f"assertions[{index}] 引用了 {factory.group(1)}.{factory.group(2)}() 的固定值，"
                        "但目标分支没有调用该工厂；透传返回不能套用另一个兜底分支的常量")
    return None


def linked_exception_message_quotes(oracle: dict[str, Any], texts: dict[str, str]) -> list[dict[str, str]]:
    """Recover literal -> direct call -> caught message -> returned object evidence.

    No call-chain guess, arbitrary getMessage(), or title is enough. Unsupported
    dispatch, nested throw paths and transformed messages remain unresolved.
    """
    from common.java_response_paths import _close, _mask, _method

    ref, separator, number = str(oracle.get("branch_id", "")).partition(":if:")
    source = texts.get(ref, "")
    method = _method(source)
    code = _mask(source)
    guards = list(re.finditer(r"\bif\s*\(", code))
    if not separator or not number.isdigit() or not method or not 0 < int(number) <= len(guards):
        return []
    guard = guards[int(number) - 1]
    opening = code.find("(", guard.start(), guard.end())
    closing = _close(code, opening, "(", ")")
    if closing is None:
        return []
    brace = closing + 1
    while brace < len(code) and code[brace].isspace():
        brace += 1
    end = _close(code, brace, "{", "}") if brace < len(code) and code[brace] == "{" else None
    if end is None or oracle.get("branch_outcome", True) is not True:
        return []
    arm = source[brace + 1:end]
    if re.search(r"\b(?:if|switch|try)\b", _mask(arm)):
        return []
    result: list[dict[str, str]] = []
    for index, assertion in enumerate(oracle.get("assertions") or []):
        if not isinstance(assertion, dict) or assertion.get("op") not in {"equals", "contains"}:
            continue
        value = assertion.get("value")
        if not isinstance(value, str):
            continue
        throws = list(re.finditer(r'\bthrow\s+new\s+([\w.]+)\s*\(\s*("(?:\\.|[^"\\])*")\s*\)\s*;', arm))
        throws = [throw for throw in throws if _literal_text(throw.group(2)) == value]
        if len(throws) != 1:
            continue
        thrown = throws[0]
        exception_type = thrown.group(1).rsplit(".", 1)[-1]
        field = str(assertion.get("path", ""))
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", field):
            continue
        setter = "set" + field[:1].upper() + field[1:]
        bindings: dict[tuple[str, str], tuple[str, str]] = {}
        for controller_ref, controller in texts.items():
            if not re.search(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\b", controller):
                continue
            masked = _mask(controller)
            for caught in re.finditer(r"\bcatch\s*\(\s*([\w.]+)\s+(\w+)\s*\)\s*\{", masked):
                if caught.group(1).rsplit(".", 1)[-1] not in {exception_type, "Exception", "Throwable"}:
                    continue
                catch_end = _close(masked, caught.end() - 1, "{", "}")
                if catch_end is None:
                    continue
                tries = list(re.finditer(r"\btry\s*\{", masked[:caught.start()]))
                linked_try = next((attempt for attempt in reversed(tries)
                                   if (try_end := _close(masked, attempt.end() - 1, "{", "}")) is not None
                                   and not masked[try_end + 1:caught.start()].strip()), None)
                if linked_try is None:
                    continue
                try_body = masked[linked_try.end():caught.start()]
                if not re.search(r"\b\w+\s*\.\s*" + re.escape(method[0]) + r"\s*\(", try_body):
                    continue
                body = controller[caught.end():catch_end]
                assignment = re.search(r"\b(\w+)\." + re.escape(setter) + r"\s*\(\s*" + re.escape(caught.group(2)) + r"\.getMessage\s*\(\s*\)\s*\)\s*;", body)
                if assignment is None or len(re.findall(r"\." + re.escape(setter) + r"\s*\(", _mask(body))) != 1:
                    continue
                following = masked[catch_end + 1:]
                returned = re.search(r"\breturn\s+" + re.escape(assignment.group(1)) + r"\s*;", following)
                if returned is None or re.search(r"\." + re.escape(setter) + r"\s*\(", following[:returned.start()]):
                    continue
                bindings.setdefault((controller, assignment.group(0)), (controller_ref, assignment.group(0)))
        if len(bindings) == 1:
            controller_ref, assignment_quote = next(iter(bindings.values()))
            result.extend([
                {"target": f"assertions[{index}]", "evidence_ref": ref, "quote": thrown.group(0)},
                {"target": f"assertions[{index}]", "evidence_ref": controller_ref, "quote": assignment_quote},
            ])
    return result


def _literal_text(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return None


def entry_assertion_issue(assertions: list[dict[str, Any]], texts: dict[str, str]) -> str | None:
    """Fixed entry values must be assigned to the returned controller object."""
    from common.java_response_paths import _close, _mask, _method

    for index, assertion in enumerate(assertions):
        if assertion.get("op") in {"exists", "null_or_missing", "equals_request"}:
            continue
        if assertion.get("op") != "equals" or not re.fullmatch(r"\w+", str(assertion.get("path", ""))):
            return f"普通入口 assertions[{index}] 尚未绑定到本控制器的直接返回赋值"
        field = str(assertion["path"])
        value = json.dumps(assertion.get("value"), ensure_ascii=False)
        assignment = re.compile(r"\b(\w+)\.set" + re.escape(field[:1].upper() + field[1:]) + r"\s*\(\s*" + re.escape(value) + r"\s*\)\s*;")
        supported = False
        for source in texts.values():
            if not re.search(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\b", source):
                continue
            for match in assignment.finditer(source):
                code = _mask(source)
                method = _method(source)
                if method is None:
                    continue
                braces: list[int] = []
                for position in range(method[2], match.start()):
                    if code[position] == "{":
                        braces.append(position)
                    elif code[position] == "}" and braces:
                        braces.pop()
                if not (len(braces) == 1 or (len(braces) == 2 and re.search(r"\btry\s*$", code[:braces[-1]]))):
                    continue
                # An error assignment inside catch cannot prove a normal entry.
                if re.search(r"\bcatch\s*\(", code[:match.start()]):
                    continue
                catch_ranges = []
                for caught in re.finditer(r"\bcatch\s*\([^)]*\)\s*\{", code):
                    catch_end = _close(code, caught.end() - 1, "{", "}")
                    if catch_end is not None:
                        catch_ranges.append((caught.start(), catch_end))
                writes = [write for write in re.finditer(
                    r"\b" + re.escape(match.group(1)) + r"\.set" + re.escape(field[:1].upper() + field[1:]) + r"\s*\(", code
                ) if not any(start <= write.start() <= end for start, end in catch_ranges)]
                if len(writes) != 1:
                    continue
                if re.search(r"\breturn\s+" + re.escape(match.group(1)) + r"\s*;", code[match.end():]):
                    supported = True
        if not supported:
            return f"普通入口 assertions[{index}] 未找到返回对象对该固定值的直接赋值"
    return None


def exception_assignment_issue(oracle: dict[str, Any], texts: dict[str, str]) -> str | None:
    """A literal in another handler cannot establish an unresolved enum getter."""
    from common.java_response_paths import _close, _mask, _method

    branch = re.fullmatch(r"(EVD-\d+):(if|catch):\d+", str(oracle.get("branch_id", "")))
    if branch is None:
        return None
    owner = texts.get(branch.group(1), "")
    method = _method(owner)
    handlers: dict[str, str] = {}
    for source in texts.values():
        if not re.search(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\b", source):
            continue
        code = _mask(source)
        if branch.group(2) == "catch":
            if source != owner:
                continue
        elif not method or not re.search(r"\b\w+\." + re.escape(method[0]) + r"\s*\(", code):
            continue
        catches = list(re.finditer(r"\bcatch\s*\([^)]*\)\s*\{", code))
        if len(catches) != 1:
            continue
        end = _close(code, catches[0].end() - 1, "{", "}")
        if end is not None:
            handlers[source] = source[catches[0].end():end]
    if len(handlers) != 1:
        return None
    source, body = next(iter(handlers.items()))
    for index, assertion in enumerate(oracle.get("assertions") or []):
        if not isinstance(assertion, dict) or assertion.get("op") != "equals":
            continue
        field = str(assertion.get("path", ""))
        if not re.fullmatch(r"\w+", field):
            continue
        setter = "set" + field[:1].upper() + field[1:]
        assignment = re.search(r"\b(\w+)\." + re.escape(setter) + r"\s*\(\s*(.*?)\s*\)\s*;", body, re.S)
        if assignment is None or not re.search(r"\breturn\s+" + re.escape(assignment.group(1)) + r"\s*;", _mask(source)):
            continue
        getter = re.fullmatch(r"([A-Z]\w*)\.([A-Z][\w]*)\.get\w+\s*\(\s*\)", assignment.group(2))
        if getter and not any(re.search(r"\b(?:enum|class)\s+" + re.escape(getter.group(1)) + r"\b", _mask(text)) for text in texts.values()):
            return (f"assertions[{index}] 实际来自 {assignment.group(2)}，当前未取得该枚举返回值定义；"
                    "其它异常处理器中的字面值不能证明本出口的固定值")
    return None

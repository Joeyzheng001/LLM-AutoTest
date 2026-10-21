"""Conservative observations of Java return paths from already collected source.

Only direct getter/setter copies are projected to JSON. Unknown expressions stay
unknown. Observations never become new expected response values.
"""
from __future__ import annotations

import json
import re
from typing import Any

from common.dto_validation import parse_java_dto_fields
from common.response_assertions import evaluate_response_assertions


def _mask(text: str) -> str:
    pattern = r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda m: "".join("\n" if c == "\n" else " " for c in m.group()), text)


def _close(text: str, start: int, left: str, right: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == left: depth += 1
        elif text[index] == right:
            depth -= 1
            if depth == 0: return index
    return None


def _method(source: str) -> tuple[str, list[str], int] | None:
    code = _mask(source)
    m = re.search(r'\b(?:public|private|protected)\s+(?:(?:static|final|synchronized)\s+)*(?:<[^{}]+?>\s+)?[\w.<>\[\],?]+\s+(\w+)\s*\(', code)
    if not m: return None
    opening = code.find('(', m.start(), m.end())
    end = _close(code, opening, '(', ')')
    if end is None: return None
    brace = code.find('{', end)
    if brace < 0: return None
    params = [re.findall(r'[A-Za-z_$][\w$]*', p)[-1] for p in source[opening+1:end].split(',') if re.findall(r'[A-Za-z_$][\w$]*', p)]
    return m.group(1), params, brace


def _json_name(field: dict[str, Any]) -> str | None:
    annotations = ' '.join(field.get('annotations', []))
    if '@JsonIgnore' in annotations: return None
    m = re.search(r'@(?:JsonProperty|JSONField)\s*\(\s*(?:name\s*=\s*)?"([^"]+)"', annotations)
    return m.group(1) if m else str(field['name'])


def _dto_fields(type_name: str, evidence: list[dict[str, Any]], seen: set[str] | None = None) -> dict[str, str]:
    seen = set(seen or ())
    if type_name in seen: return {}
    seen.add(type_name)
    matches = [e for e in evidence if str(e.get('file', '')).endswith('/'+type_name+'.java') and e.get('role') == 'response_dto']
    if len({str(e.get('file')) for e in matches}) != 1:
        return {}
    fields: dict[str, str] = {}
    for e in matches:
        text = str(e.get('excerpt', ''))
        for parent in re.findall(r'\bextends\s+(\w+)', _mask(text)):
            fields.update(_dto_fields(parent, evidence, seen))
        for name, field in parse_java_dto_fields(text).items():
            json_name = _json_name(field)
            if json_name:
                fields[name[:1].upper()+name[1:]] = json_name
    return fields


def _early_returns(source: str, before: int) -> list[tuple[str, str]]:
    method = _method(source)
    if not method: return []
    code = _mask(source)
    base = method[2]
    out: list[tuple[str, str]] = []
    for m in re.finditer(r'\bif\s*\(', code[:before]):
        if m.start() < base: continue
        if code[base:m.start()].count('{')-code[base:m.start()].count('}') != 1: continue
        opening = code.find('(', m.start(), m.end())
        end = _close(code, opening, '(', ')')
        if end is None: continue
        brace = end+1
        while brace < len(code) and code[brace].isspace(): brace += 1
        if brace >= len(code) or code[brace] != '{': continue
        close = _close(code, brace, '{', '}')
        if close is None or close >= before: continue
        body = code[brace+1:close]
        # Nested or conditional exits do not prove the outer guard must be false.
        if '{' in body or not re.search(r'\b(?:return|throw)\b', body): continue
        out.append((source[opening+1:end], source[m.start():end+1]))
    return out


def response_path_observation(oracle: dict[str, Any], interface: dict[str, Any], evidence: list[dict[str, Any]], body: Any) -> dict[str, Any]:
    branch_id = str(oracle.get('branch_id', ''))
    ref, _, index_text = branch_id.partition(':if:')
    target = next((e for e in evidence if e.get('evidence_id') == ref), None)
    result: dict[str, Any] = {'status': 'unconfirmed', 'conditions': [], 'unknown_conditions': []}
    if target is None or not index_text.isdigit(): return result
    text = str(target.get('excerpt', '')); code = _mask(text)
    guards = list(re.finditer(r'\bif\s*\(', code))
    index = int(index_text)-1
    if index < 0 or index >= len(guards): return result
    opening = code.find('(', guards[index].start(), guards[index].end())
    closing = _close(code, opening, '(', ')')
    if closing is None: return result
    contexts: list[tuple[dict[str, Any], str, str, bool]] = [(target, text[opening+1:closing], text[guards[index].start():closing+1], bool(oracle.get('branch_outcome', True)))]
    contexts += [(target, expr, quote, False) for expr, quote in _early_returns(text, guards[index].start())]
    method = _method(text)
    if method:
        callers: dict[str, tuple[dict[str, Any], int]] = {}
        for e in evidence:
            source = str(e.get('excerpt', ''))
            own = _method(source)
            if e.get('file') != target.get('file') or not own or own[0] == method[0]: continue
            call = re.search(r'(?<![.\w])'+re.escape(method[0])+r'\s*\(', _mask(source))
            if call: callers[source] = (e, call.start())
        if len(callers) == 1:
            caller, position = next(iter(callers.values()))
            contexts += [(caller, expr, quote, False) for expr, quote in _early_returns(str(caller['excerpt']), position)]
    root_type = str((interface.get('returns') or {}).get('type', '')).rsplit('.',1)[-1]
    fields = _dto_fields(root_type, evidence)
    if not fields: return result
    # A global exception envelope is not the return object produced by the
    # copied DTO. Its non-null body cannot prove a Service value was non-null.
    if not isinstance(body, dict) or not set(fields.values()).intersection(body):
        return result
    # Collect only source-evidenced direct copies and actual caller bindings.
    bridges: list[tuple[str, str, int, int, set[str], str]] = []
    for e in evidence:
        source = str(e.get('excerpt', '')); info = _method(source)
        if not info: continue
        copies = list(re.finditer(r'\b(\w+)\.set(\w+)\s*\(\s*(\w+)\.get\2\s*\(\s*\)\s*\)', source))
        for copy in copies:
            dest, prop, src = copy.groups()
            if src not in info[1]: continue
            returning = bool(re.search(r'\breturn\s+'+re.escape(dest)+r'\s*;', source))
            if returning:
                bridges.append((info[0], str(e.get('file','')), info[1].index(src), -1, {c.group(2) for c in copies if c.group(1)==dest and c.group(3)==src}, str(e.get('evidence_id',''))))
            elif dest in info[1]:
                bridges.append((info[0], str(e.get('file','')), info[1].index(src), info[1].index(dest), {c.group(2) for c in copies if c.group(1)==dest and c.group(3)==src}, str(e.get('evidence_id',''))))
    observed: list[dict[str, Any]] = []; unknown: list[str] = []
    for source_evidence, expression, quote, desired in contexts:
        source = str(source_evidence.get('excerpt',''))
        roots: dict[str, set[str]] = {}
        for name, file, src_index, dest_index, props, bridge_ref in bridges:
            if source_evidence.get('file') != file: continue
            for call in re.finditer(r'\b'+re.escape(name)+r'\s*\(([^()]*)\)', source):
                args = [a.strip() for a in call.group(1).split(',')]
                if src_index >= len(args): continue
                if dest_index == -1:
                    prefix = source[max(0,call.start()-100):call.start()]
                    assignment = re.search(r'\b(\w+)\s*=\s*$', prefix)
                    copied_root = bool(assignment and re.search(r'\breturn\s+'+re.escape(assignment.group(1))+r'\s*;', source))
                else:
                    copied_root = dest_index < len(args) and bool(re.search(r'\breturn\s+'+re.escape(args[dest_index])+r'\s*;', source))
                if copied_root and re.fullmatch(r'\w+', args[src_index]): roots.setdefault(args[src_index],set()).update(props)
        aliases = {m.group(1):m.group(2).strip() for m in re.finditer(r'\b(\w+)\s*=(?!=)\s*([^;]+);', source)}
        def root_properties(receiver: str) -> set[str] | None:
            seen: set[str] = set()
            while receiver not in seen:
                seen.add(receiver)
                if receiver in roots:
                    return roots[receiver]
                receiver = aliases.get(receiver, '')
                if not re.fullmatch(r'\w+', receiver):
                    break
            return None
        def project(value: str, trail: set[str] | None = None) -> str | None:
            trail = set(trail or ())
            value = value.strip()
            if value in trail: return None
            trail.add(value)
            if value in roots: return ''
            if value in aliases: return project(aliases[value],trail)
            getter = re.fullmatch(r'(\w+)\.get(\w+)\s*\(\s*\)',value)
            if not getter: return None
            receiver, prop = getter.groups()
            parent = project(receiver,trail)
            if parent is None: return None
            if not parent:
                copied = root_properties(receiver)
                if copied is None or prop not in copied: return None
                field = fields.get(prop)
            else:
                names = {_json_name(f) for e in evidence if e.get('role')=='response_dto'
                         for f in parse_java_dto_fields(str(e.get('excerpt',''))).values()
                         if str(f['name']).lower()==prop.lower()}
                names.discard(None)
                field = next(iter(names)) if len(names)==1 else None
            return (parent+'.' if parent else '')+str(field) if field else None
        expr = expression.strip(); condition: dict[str,Any] | None = None
        null = re.fullmatch(r'(\w+)\s*(==|!=)\s*null',expr)
        equal = re.fullmatch(r'(!?)\s*("(?:\\.|[^"\\])*"|[A-Z_][A-Z0-9_]*)\.equals\s*\(\s*(\w+\.get\w+\s*\(\s*\))\s*\)',expr)
        if null:
            path = project(null.group(1))
            if path is not None: condition={'path':path or '$','op':'equals' if (null.group(2)=='==')==desired else 'not_equals','value':None}
        elif equal:
            path=project(equal.group(3)); raw=equal.group(2)
            if not raw.startswith('"'):
                values={m.group(1) for e in evidence if e.get('file')==source_evidence.get('file') for m in re.finditer(r'\b'+re.escape(raw)+r'\s*=\s*"([^"\n]*)"',str(e.get('excerpt','')))}
                raw=json.dumps(next(iter(values))) if len(values)==1 else ''
            if path is not None and raw:
                expected=json.loads(raw)
                # Later mutation can make a returned value ambiguous. A known
                # literal overwrite only masks that literal, never other values.
                getter_match = re.search(r'get(\w+)\s*\(',equal.group(3))
                assert getter_match is not None  # guaranteed by the fullmatch above
                prop=getter_match.group(1)
                writes=[m.group(1).strip() for e in evidence for m in re.finditer(r'\.set'+re.escape(prop)+r'\s*\(([^;]*)\)\s*;',str(e.get('excerpt','')))]
                ambiguous=[]
                for write in writes:
                    if re.fullmatch(r'\w+\.get'+re.escape(prop)+r'\s*\(\s*\)',write): continue
                    try: ambiguous.append(json.loads(write))
                    except (ValueError,TypeError): ambiguous.append('__dynamic__')
                from common.response_assertions import _resolve_json_path
                found, actual=_resolve_json_path(body,path)
                if found and '__dynamic__' not in ambiguous and actual not in ambiguous:
                    condition={'path':path,'op':'not_equals' if bool(equal.group(1))==desired else 'equals','value':expected}
        if condition is None:
            unknown.append(str(source_evidence.get('evidence_id'))+': '+quote);continue
        outcomes=evaluate_response_assertions(body if condition['path']!='$' else {'root':body},[{**condition,'path':condition['path'] if condition['path']!='$' else 'root'}])
        if not outcomes[0]['found']:
            unknown.append(str(source_evidence.get('evidence_id'))+': '+quote)
            continue
        observed.append({'condition':condition,'passed':outcomes[0]['passed'],'actual':outcomes[0]['actual'],
                         'evidence_ref':source_evidence['evidence_id'],'quote':quote,'required_outcome':desired})
    result['conditions']=observed;result['unknown_conditions']=unknown
    if any(not c['passed'] for c in observed): result['status']='not_reached'
    elif observed and not unknown: result['status']='matched_observable_path'
    return result

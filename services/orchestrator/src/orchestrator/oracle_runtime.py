"""Runtime observation for fixed, source-backed API exception probes."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx


# 仅这些请求可由当前源码证明会在调用有副作用的下游业务前拒绝；运行实测必须显式开启。
# 请求体固定为合成无效值，不包含生产数据或数据库记录标识。
_SAFE_RUNTIME_EXCEPTION_PROBES: dict[str, dict[str, Any]] = {
    "IF-REST-001-accountNo-blank": {
        "interface_id": "IF-REST-001",
        "path": "/risk/demand-deposit/trial",
        "body": {"accountNo": ""},
        "scenario": "accountNo为空字符串时，接口拒绝请求",
        "preconditions": [
            {"path": "param.accountNo", "op": "equals", "value": ""},
        ],
        "source_quotes": [
            {"target": "preconditions[0]", "role": "called_method",
             "quote": "StringUtils.isBlank(param.getString(ACCOUNT_NO))"},
        ],
        "required_source": [
            "validateBusinessParams(param);",
            "StringUtils.isBlank(param.getString(ACCOUNT_NO))",
        ],
        "required_source_sequences": [{
            "role": "controller",
            "quotes": [
                "validateBusinessParams(param);",
                "standardRiskTrialService.trial(param, ExecutorTypeEnum.O32)",
            ],
        }],
    },
    "IF-REST-002-invalid-stage": {
        "interface_id": "IF-REST-002",
        "path": "/risk/approval/trial",
        "body": {"autoApprovalFlag": "1", "autoApprovalStage": "99"},
        "scenario": "开启自动审批且阶段值不在允许范围时，接口拒绝请求",
        "preconditions": [
            {"path": "param.autoApprovalFlag", "op": "equals", "value": "1"},
            {"path": "param.autoApprovalStage", "op": "equals", "value": "99"},
        ],
        "source_quotes": [
            {"target": "preconditions[0]", "role": "called_method",
             "quote": '"1".equals(StringUtils.trim(param.getString("autoApprovalFlag")))'},
            {"target": "preconditions[1]", "role": "called_method",
             "quote": "AUTO_APPROVAL_STAGES.contains(stage)"},
            {"target": "preconditions[1]", "role": "called_method_constant",
             "quote": 'Set.of("00", "01", "02")'},
        ],
        "required_source": [
            "RiskApprovalRequestValidator.validate(param);",
            "AUTO_APPROVAL_STAGES.contains(stage)",
        ],
        "required_source_sequences": [{
            "role": "controller",
            "quotes": [
                "RiskApprovalRequestValidator.validate(param);",
                "riskTrialSymbolResolver.resolve(param);",
                "riskApprovalService.trial(param)",
            ],
        }],
    },
    "IF-REST-002-invalid-ex-destination": {
        "interface_id": "IF-REST-002",
        "path": "/risk/approval/trial",
        "body": {"GroupList": [{
            "BusinType": "H1",
            "Symbol": "RUNTIME-PROBE-SYMBOL",
            "TempSymbol": "RUNTIME-PROBE-TEMP",
            "ExDestination": "INVALID",
        }]},
        "scenario": "GroupList中的ExDestination不受支持时，接口在下游试算前拒绝请求",
        "preconditions": [
            {"path": "param.GroupList[0].BusinType", "op": "equals", "value": "H1"},
            {"path": "param.GroupList[0].Symbol", "op": "equals", "value": "RUNTIME-PROBE-SYMBOL"},
            {"path": "param.GroupList[0].TempSymbol", "op": "equals", "value": "RUNTIME-PROBE-TEMP"},
            {"path": "param.GroupList[0].ExDestination", "op": "equals", "value": "INVALID"},
        ],
        "source_quotes": [
            {"target": "preconditions[0]", "role": "called_method",
             "quote": '!"H1".equals(businessType) && !"H2".equals(businessType)'},
            {"target": "preconditions[1]", "role": "called_method",
             "quote": 'String symbol = item.getString("Symbol");'},
            {"target": "preconditions[1]", "role": "called_method",
             "quote": "if (StringUtils.isBlank(symbol) || StringUtils.isBlank(tempSymbol)"},
            {"target": "preconditions[1]", "role": "called_method",
             "quote": "|| StringUtils.equals(symbol, tempSymbol)) {"},
            {"target": "preconditions[2]", "role": "called_method",
             "quote": 'String tempSymbol = item.getString("TempSymbol");'},
            {"target": "preconditions[2]", "role": "called_method",
             "quote": "if (StringUtils.isBlank(symbol) || StringUtils.isBlank(tempSymbol)"},
            {"target": "preconditions[2]", "role": "called_method",
             "quote": "|| StringUtils.equals(symbol, tempSymbol)) {"},
            {"target": "preconditions[3]", "role": "called_method",
             "quote": 'String exDestination = StringUtils.trimToEmpty(item.getString("ExDestination"));'},
            {"target": "preconditions[3]", "role": "called_method_constant",
             "quote": 'Map.of("SS", 1, "SZ", 2, "OTC", 5)'},
            {"target": "preconditions[3]", "role": "called_method",
             "quote": 'throw new RiskServiceException("临时代码查询要求 ExDestination 为 SS、SZ 或 OTC，detailIndex="'},
        ],
        "required_source": [
            'RiskApprovalRequestValidator.validate(param);',
            'riskTrialSymbolResolver.resolve(param);',
            'if (mktNo == null)',
            'throw new RiskServiceException("临时代码查询要求 ExDestination 为 SS、SZ 或 OTC',
        ],
        "required_source_sequences": [{
            "role": "controller",
            "quotes": [
                "RiskApprovalRequestValidator.validate(param);",
                "riskTrialSymbolResolver.resolve(param);",
                "riskApprovalService.trial(param)",
            ],
        }],
    },
    "IF-REST-003-unsupported-risk-channel": {
        "interface_id": "IF-REST-003",
        "path": "/risk/complex/order/trial",
        "body": {"riskChannel": 99},
        "scenario": "riskChannel为99时，Service在下游执行前拒绝请求",
        "preconditions": [
            {"path": "param.riskChannel", "op": "equals", "value": 99},
        ],
        "source_quotes": [
            {"target": "preconditions[0]", "role": "called_method",
             "quote": 'throw new RuntimeException("Risk Channel Not Supported")'},
            {"target": "preconditions[0]", "role": "called_method_constant",
             "quote": "RISK_CHANNEL_ALL = 0"},
            {"target": "preconditions[0]", "role": "called_method_constant",
             "quote": "RISK_CHANNEL_STD = 1"},
            {"target": "preconditions[0]", "role": "called_method_constant",
             "quote": "RISK_CHANNEL_O32 = 2"},
        ],
        "required_source": [
            "Risk Channel Not Supported",
            "resolveComplexExecutors(param)",
        ],
        "required_source_sequences": [{
            "role": "service_impl",
            "quotes": [
                "CollectionUtils.isEmpty(executorProperties.getComplexExecutors())",
                "return executeTrial(param, resolveComplexExecutors(param));",
            ],
        }],
    },
    "IF-REST-004-invalid-request-type": {
        "interface_id": "IF-REST-004",
        "path": "/risk/standard/order/{code}",
        "path_values": {"code": "codex-invalid-runtime-probe"},
        "body": {},
        "scenario": "请求类型 code 不受支持时，Controller拒绝请求",
        "preconditions": [
            {"path": "code", "op": "equals", "value": "codex-invalid-runtime-probe"},
            {"path": "param", "op": "exists"},
        ],
        "source_quotes": [
            {"target": "preconditions[0]", "role": "controller",
             "quote": "RequestTypeEnum.parse(code)"},
            {"target": "preconditions[0]", "role": "controller",
             "quote": 'throw new RuntimeException("Request Type Not Supported")'},
            {"target": "preconditions[0]", "role": "called_method",
             "quote": "Objects.equals(code, value.getCode())"},
            {"target": "preconditions[0]", "role": "enum_definition",
             "quote": 'TRIAL("trial")'},
            {"target": "preconditions[1]", "role": "controller",
             "quote": "@RequestBody JSONObject param"},
        ],
        "required_source": [
            "RequestTypeEnum.parse(code)",
            "Request Type Not Supported",
        ],
        "required_source_sequences": [{
            "role": "controller",
            "quotes": [
                "RequestTypeEnum requestType = RequestTypeEnum.parse(code);",
                "if (requestType == null)",
                'throw new RuntimeException("Request Type Not Supported")',
                "Object result = service.execute(requestType, param);",
            ],
        }],
    },
}

async def _observe_safe_runtime_exception_contracts(
    rest_base_url: str,
    interfaces: list[dict[str, Any]],
    source_evidence: list[dict[str, Any]],
    *,
    verify: bool,
    auth: dict[str, Any] | None,
    auth_headers: dict[str, str] | None,
    first_evidence_index: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """确认运行时路由后，重复观测源码已证明无副作用的固定异常请求。"""
    by_id = {str(item.get("id", "")): item for item in interfaces}
    headers = dict(auth_headers or {})
    warnings: list[str] = []
    authenticated = bool(headers)

    base = rest_base_url.rstrip("/") + "/"
    observations: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=verify, headers=headers) as client:
            openapi_response = await client.get(urljoin(base, "v3/api-docs"))
            if openapi_response.status_code in {401, 403} and auth and not authenticated:
                from executor.nodes.auto_execute import login_for_headers

                try:
                    headers = await login_for_headers(auth)
                except Exception as exc:
                    return [], [f"运行实测契约未执行：鉴权准备失败（{type(exc).__name__}）"]
                client.headers.update(headers)
                authenticated = True
                openapi_response = await client.get(urljoin(base, "v3/api-docs"))
            openapi_response.raise_for_status()
            openapi = openapi_response.json()
            openapi_paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}
            for probe_id, spec in _SAFE_RUNTIME_EXCEPTION_PROBES.items():
                interface_id = str(spec.get("interface_id", ""))
                interface = by_id.get(interface_id)
                if not isinstance(interface, dict):
                    continue
                signature = interface.get("signature") or {}
                source_path = str(signature.get("path", ""))
                if (
                    str(signature.get("method", "")).upper() != "POST"
                    or source_path != spec["path"]
                    or not isinstance(openapi_paths.get(source_path), dict)
                    or "post" not in openapi_paths[source_path]
                ):
                    warnings.append(
                        f"接口 {interface_id} 未做运行实测：OpenAPI 路径与源码接口不一致或未发布"
                    )
                    continue
                source_text = "\n".join(
                    str(entry.get("excerpt", ""))
                    for entry in source_evidence
                    if interface_id in (entry.get("interface_ids") or [])
                    and entry.get("category") != "runtime_observation"
                )
                input_issue = _runtime_probe_input_issue(spec)
                if input_issue:
                    warnings.append(
                        f"接口 {interface_id} 未做运行实测：固定探测配置无效（{input_issue}）"
                    )
                    continue
                required_source = spec.get("required_source") or []
                if any(str(needle) not in source_text for needle in required_source):
                    warnings.append(
                        f"接口 {interface_id} 未做运行实测：源码未能证明固定探测请求会在业务调用前拒绝"
                    )
                    continue
                source_order_issue = _runtime_probe_source_order_issue(
                    spec, interface_id, source_evidence
                )
                if source_order_issue:
                    warnings.append(
                        f"接口 {interface_id} 未做运行实测：源码调用顺序不能证明探测会先于业务调用失败（{source_order_issue}）"
                    )
                    continue
                source_quotes, source_issue = _resolve_runtime_probe_source_quotes(
                    spec, interface_id, source_evidence
                )
                if source_issue:
                    warnings.append(
                        f"接口 {interface_id} 未做运行实测：探测前置条件缺少逐项源码证据（{source_issue}）"
                    )
                    continue
                request_path = source_path
                for key, value in (spec.get("path_values") or {}).items():
                    request_path = request_path.replace("{" + str(key) + "}", str(value))
                url = urljoin(base, request_path.lstrip("/"))
                request_body = spec["body"]
                responses: list[tuple[int, str, dict[str, Any]]] = []
                for _ in range(2):
                    response = await client.post(url, json=request_body)
                    if response.status_code in {401, 403} and auth and not authenticated:
                        from executor.nodes.auto_execute import login_for_headers

                        try:
                            headers = await login_for_headers(auth)
                        except Exception as exc:
                            warnings.append(
                                f"接口 {interface_id} 未形成实测契约：鉴权准备失败（{type(exc).__name__}）"
                            )
                            responses = []
                            break
                        client.headers.update(headers)
                        authenticated = True
                        response = await client.post(url, json=request_body)
                    content_type = response.headers.get("content-type", "")
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        responses = []
                        break
                    responses.append((response.status_code, content_type, body))
                if len(responses) != 2 or responses[0] != responses[1]:
                    warnings.append(
                        f"接口 {interface_id} 未形成稳定运行实测契约：两次响应不一致或不是 JSON 对象"
                    )
                    continue
                status, content_type, body = responses[0]
                if not content_type.lower().startswith("application/json"):
                    warnings.append(f"接口 {interface_id} 未形成运行实测契约：响应不是 JSON")
                    continue
                response_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                request_json = json.dumps(
                    request_body, ensure_ascii=False, separators=(",", ":")
                )
                evidence = {
                    "evidence_id": f"EVD-{first_evidence_index + len(observations):04d}",
                    "category": "runtime_observation",
                    "repo": "runtime-test-environment",
                    "file": url,
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt": (
                        f"请求：POST {request_path}\n请求 JSON：{request_json}\n"
                        f"HTTP status={status}\nContent-Type: {content_type}\n"
                        f"响应 JSON：{response_json}\n重复观测次数：2"
                    ),
                    "excerpt_truncated": False,
                    "interface_ids": [interface_id],
                    "role": "runtime_contract",
                }
                observations.append({
                    "probe_id": probe_id,
                    "interface_id": interface_id,
                    "path": request_path,
                    "request_body": request_body,
                    "scenario": str(spec.get("scenario", "")),
                    "preconditions": spec.get("preconditions") or [],
                    "source_evidence_quotes": source_quotes,
                    "status": status,
                    "content_type": content_type,
                    "response_body": body,
                    "evidence": evidence,
                })
    except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
        warnings.append(
            f"运行实测契约探测失败（{type(exc).__name__}）；未使用未验证的响应生成断言"
        )
    return observations, warnings


def _runtime_probe_source_order_issue(
    spec: dict[str, Any],
    interface_id: str,
    source_evidence: list[dict[str, Any]],
) -> str | None:
    """Require ordered, same-method source evidence before issuing a probe."""
    sequences = spec.get("required_source_sequences")
    if not isinstance(sequences, list) or not sequences:
        return "没有配置 required_source_sequences"
    for sequence_index, sequence in enumerate(sequences):
        if not isinstance(sequence, dict):
            return f"required_source_sequences[{sequence_index}] 结构不合法"
        role = str(sequence.get("role", ""))
        quotes = sequence.get("quotes")
        if not role or not isinstance(quotes, list) or not quotes or any(
            not isinstance(quote, str) or not quote for quote in quotes
        ):
            return f"required_source_sequences[{sequence_index}] 缺少 role/quotes"
        excerpts = [
            str(item.get("excerpt", ""))
            for item in source_evidence
            if item.get("role") == role
            and interface_id in (item.get("interface_ids") or [])
            and item.get("category") != "runtime_observation"
        ]
        ordered_in_one_method = False
        for excerpt in excerpts:
            cursor = 0
            for quote in quotes:
                found = excerpt.find(quote, cursor)
                if found < 0:
                    break
                cursor = found + len(quote)
            else:
                ordered_in_one_method = True
                break
        if not ordered_in_one_method:
            return f"源码片段未按顺序证明 {role} 的调用与提前退出"
    return None


def _runtime_probe_input_issue(spec: dict[str, Any]) -> str | None:
    """Ensure a fixed probe's declared preconditions exactly match its HTTP input."""
    body = spec.get("body")
    path_values = spec.get("path_values") or {}
    conditions = spec.get("preconditions")
    if not isinstance(body, dict) or not isinstance(path_values, dict) or not isinstance(conditions, list):
        return "body、path_values 或 preconditions 结构不合法"
    roots: dict[str, Any] = {"param": body, **path_values}
    for index, condition in enumerate(conditions):
        if not isinstance(condition, dict):
            return f"preconditions[{index}] 不是对象"
        path = str(condition.get("path", ""))
        if not re.fullmatch(
            r"[A-Za-z_$][\w$-]*(?:\[\d+\])*(?:\.[A-Za-z_$][\w$-]*(?:\[\d+\])*)*",
            path,
        ):
            return f"preconditions[{index}] 路径格式无效：{path}"
        tokens = re.findall(r"[A-Za-z_$][\w$-]*|\[\d+\]", path)
        root = tokens[0]
        if root not in roots:
            return f"preconditions[{index}] 根字段未出现在请求中：{root}"
        value = roots[root]
        for token in tokens[1:]:
            if not token.startswith("["):
                name = token
                if not isinstance(value, dict) or name not in value:
                    return f"preconditions[{index}] 字段未出现在请求中：{path}"
                value = value[name]
            else:
                position = int(token[1:-1])
                if not isinstance(value, list) or position >= len(value):
                    return f"preconditions[{index}] 数组项未出现在请求中：{path}"
                value = value[position]
        op = condition.get("op")
        if op == "exists":
            continue
        if op == "equals":
            expected = condition.get("value")
            if type(value) is not type(expected) or value != expected:
                return f"preconditions[{index}] 与请求值不一致：{path}"
            continue
        return f"preconditions[{index}] 使用了不支持的操作：{op}"
    return None


def _resolve_runtime_probe_source_quotes(
    spec: dict[str, Any],
    interface_id: str,
    source_evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], str | None]:
    """Resolve every fixed probe precondition to a role-scoped source excerpt."""
    preconditions = spec.get("preconditions") or []
    raw_quotes = spec.get("source_quotes") or []
    if not isinstance(preconditions, list) or not isinstance(raw_quotes, list):
        return [], "preconditions/source_quotes 不是数组"
    normalized: list[dict[str, str]] = []
    covered_targets: set[str] = set()
    for index, quote in enumerate(raw_quotes):
        if not isinstance(quote, dict):
            return [], f"source_quotes[{index}] 不是对象"
        target = str(quote.get("target", ""))
        role = str(quote.get("role", ""))
        needle = str(quote.get("quote", ""))
        target_match = re.fullmatch(r"preconditions\[(\d+)\]", target)
        if not target_match or int(target_match.group(1)) >= len(preconditions) or not role or not needle:
            return [], f"source_quotes[{index}] target/role/quote 无效"
        source = next((
            item for item in source_evidence
            if item.get("role") == role
            and interface_id in (item.get("interface_ids") or [])
            and item.get("category") != "runtime_observation"
            and needle in str(item.get("excerpt", ""))
        ), None)
        if source is None:
            return [], needle
        evidence_id = source.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            return [], "源码证据缺少 evidence_id"
        covered_targets.add(target)
        normalized.append({
            "target": target,
            "evidence_ref": evidence_id,
            "quote": needle,
        })
    required_targets = {f"preconditions[{index}]" for index in range(len(preconditions))}
    missing = sorted(required_targets - covered_targets)
    if missing:
        return [], ", ".join(missing)
    return normalized, None


def _derive_runtime_observed_exception_oracles(
    interfaces: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind each observed response to its exact probe inputs and source branch."""
    interface_by_id = {
        str(item.get("id", "")): item for item in interfaces if item.get("id")
    }
    oracles: list[dict[str, Any]] = []
    warnings: list[str] = []
    for observed in observations:
        interface_id = str(observed.get("interface_id", ""))
        probe_id = str(observed.get("probe_id", ""))
        spec = _SAFE_RUNTIME_EXCEPTION_PROBES.get(probe_id)
        if (
            spec is None
            or str(spec.get("interface_id", "")) != interface_id
            or interface_id not in interface_by_id
        ):
            continue
        if (
            _runtime_probe_input_issue(spec)
            or observed.get("request_body") != spec.get("body")
            or observed.get("preconditions") != spec.get("preconditions")
        ):
            warnings.append(
                f"接口 {interface_id} 的运行实测预期未生成：观测请求与固定探测前置条件不一致"
            )
            continue
        expected_path = str((interface_by_id[interface_id].get("signature") or {}).get("path", ""))
        for key, value in (spec.get("path_values") or {}).items():
            expected_path = expected_path.replace("{" + str(key) + "}", str(value))
        if observed.get("path") != expected_path:
            warnings.append(
                f"接口 {interface_id} 的运行实测预期未生成：观测路径与固定探测路径不一致"
            )
            continue
        runtime_evidence = observed.get("evidence")
        response_body = observed.get("response_body")
        if not isinstance(runtime_evidence, dict) or not isinstance(response_body, dict):
            continue
        runtime_ref = runtime_evidence.get("evidence_id")
        if not isinstance(runtime_ref, str) or not runtime_ref:
            warnings.append(f"接口 {interface_id} 的运行实测预期未生成：响应证据缺少 evidence_id")
            continue
        status = observed.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            warnings.append(f"接口 {interface_id} 的运行实测预期未生成：HTTP 状态码无效")
            continue
        request_json = json.dumps(spec["body"], ensure_ascii=False, separators=(",", ":"))
        response_json = json.dumps(response_body, ensure_ascii=False, separators=(",", ":"))
        runtime_excerpt = str(runtime_evidence.get("excerpt", ""))
        if (
            runtime_evidence.get("category") != "runtime_observation"
            or runtime_evidence.get("role") != "runtime_contract"
            or f"请求：POST {expected_path}" not in runtime_excerpt
            or f"请求 JSON：{request_json}" not in runtime_excerpt
            or f"HTTP status={status}" not in runtime_excerpt
            or f"响应 JSON：{response_json}" not in runtime_excerpt
        ):
            warnings.append(
                f"接口 {interface_id} 的运行实测预期未生成：响应证据与观测记录不一致"
            )
            continue
        source_evidence_quotes = observed.get("source_evidence_quotes")
        required_targets = {
            f"preconditions[{index}]"
            for index in range(len(spec.get("preconditions") or []))
        }
        if (
            not isinstance(source_evidence_quotes, list)
            or any(
                not isinstance(item, dict) or str(item.get("target", "")) not in required_targets
                for item in source_evidence_quotes
            )
            or not required_targets.issubset({
                str(item.get("target", ""))
                for item in source_evidence_quotes if isinstance(item, dict)
            })
        ):
            warnings.append(
                f"接口 {interface_id} 的运行实测预期未生成：前置条件缺少逐项源码引用"
            )
            continue
        evidence_refs = [runtime_ref]
        evidence_quotes: list[dict[str, str]] = [{
            "target": "expected_status",
            "evidence_ref": runtime_ref,
            "quote": f"HTTP status={status}",
        }]
        for source_quote in source_evidence_quotes:
            if not isinstance(source_quote, dict):
                evidence_quotes = []
                break
            target = str(source_quote.get("target", ""))
            evidence_ref = str(source_quote.get("evidence_ref", ""))
            needle = str(source_quote.get("quote", ""))
            source = next((item for item in code_evidence if str(item.get("evidence_id", "")) == evidence_ref), None)
            if (
                source is None
                or interface_id not in (source.get("interface_ids") or [])
                or source.get("category") == "runtime_observation"
                or needle not in str(source.get("excerpt", ""))
            ):
                warnings.append(
                    f"接口 {interface_id} 的运行实测预期未生成：缺少源码证据 {needle}"
                )
                evidence_quotes = []
                break
            ref = str(source["evidence_id"])
            evidence_refs.append(ref)
            evidence_quotes.append({"target": target, "evidence_ref": ref, "quote": needle})
        if not evidence_quotes:
            continue
        assertions: list[dict[str, Any]] = []
        for path in ("returnCode", "returnMsg", "returnType", "body"):
            if path not in response_body:
                continue
            value = response_body[path]
            fragment = (
                json.dumps(path, ensure_ascii=False)
                + ":"
                + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            )
            assertions.append({"path": path, "op": "equals", "value": value})
            evidence_quotes.append({
                "target": f"assertions[{len(assertions) - 1}]",
                "evidence_ref": runtime_ref,
                "quote": fragment,
            })
        if not assertions:
            warnings.append(f"接口 {interface_id} 的运行实测响应没有稳定可断言字段")
            continue
        evidence_refs = list(dict.fromkeys(evidence_refs))
        oracles.append({
            "interface_id": interface_id,
            "category": "exception",
            "scenario": str(observed.get("scenario") or spec.get("scenario") or "运行实测异常分支"),
            "preconditions": observed.get("preconditions") or [],
            "expected_status": f"status={status}",
            "assertions": assertions,
            "evidence_refs": evidence_refs,
            "evidence_quotes": evidence_quotes,
        })
    return oracles, warnings



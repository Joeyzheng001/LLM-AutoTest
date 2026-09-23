"""离线 golden 评估：区分结构合规与语义质量。

该模块只读取已经落盘的产物，不调用模型、不访问被测服务，也不修改 job。
它用于比较 Prompt/模型版本的回归质量，避免把“JSON 能解析”误当成“结果正确”。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_REQUIRED_FILES = ("requirement_items.json", "requirement_review.json", "review_mapping.json")
_WS_RE = re.compile(r"\s+")


def evaluate_artifacts(
    artifact_dir: Path, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """评估一个 job 产物目录，返回可持久化的指标报告。

    ``manifest`` 可声明期望需求 ID 和阈值。未声明的可选指标返回 ``null``，
    不会因为缺少人工标注而伪造一个通过率。
    """
    manifest = manifest or {}
    payloads: dict[str, dict[str, Any]] = {}
    structural_errors: list[str] = []
    for filename in _REQUIRED_FILES:
        path = artifact_dir / filename
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            structural_errors.append(f"缺少 {filename}")
            continue
        except (OSError, json.JSONDecodeError) as exc:
            structural_errors.append(f"{filename} 无法读取：{type(exc).__name__}")
            continue
        if not isinstance(value, dict):
            structural_errors.append(f"{filename} 顶层不是对象")
            continue
        payloads[filename] = value

    required_arrays = {
        "requirement_items.json": "items",
        "requirement_review.json": "issues",
        "review_mapping.json": "mappings",
    }
    for filename, key in required_arrays.items():
        payload = payloads.get(filename)
        if payload is not None and not isinstance(payload.get(key), list):
            structural_errors.append(f"{filename} 缺少 {key} 数组")

    items = _list_of_dicts(payloads.get("requirement_items.json", {}).get("items"))
    review = payloads.get("review_mapping.json", {})
    mappings = _list_of_dicts(review.get("mappings"))
    source_catalog = _list_of_dicts(
        payloads.get("requirement_items.json", {}).get("source_catalog")
    )
    source_ids = {
        str(source.get("source_id")) for source in source_catalog if source.get("source_id")
    }
    evidence_catalog = _list_of_dicts(review.get("evidence_catalog"))
    evidence_ids = {
        str(evidence.get("evidence_id"))
        for evidence in evidence_catalog
        if evidence.get("evidence_id")
    }

    requirement_ids = {str(item.get("id")) for item in items if item.get("id")}
    expected_ids = {
        str(value) for value in manifest.get("expected_requirement_ids", []) if value
    }
    coverage = _ratio(len(requirement_ids & expected_ids), len(expected_ids))

    traceable_items = 0
    for item in items:
        refs = _string_list(item.get("source_refs"))
        claims = _list_of_dicts(item.get("claims"))
        claim_refs = [ref for claim in claims for ref in _string_list(claim.get("source_refs"))]
        all_refs = refs + claim_refs
        if all_refs and source_ids and set(all_refs) <= source_ids:
            traceable_items += 1
    traceability = _ratio(traceable_items, len(items))

    normalized_titles = [_normalize(str(item.get("title", ""))) for item in items]
    non_empty_titles = [title for title in normalized_titles if title]
    duplicate_count = len(non_empty_titles) - len(set(non_empty_titles))
    duplicate_rate = _ratio(max(duplicate_count, 0), len(non_empty_titles))

    eligible_mappings = [
        mapping
        for mapping in mappings
        if str(mapping.get("status")) in {"implemented", "partial"}
    ]
    no_evidence_count = 0
    for mapping in eligible_mappings:
        refs = set(_string_list(mapping.get("evidence_refs")))
        evidence = mapping.get("evidence")
        if isinstance(evidence, dict):
            refs.update(_string_list(evidence.get("refs")))
        assessments = _list_of_dicts(mapping.get("claim_assessments"))
        refs.update(
            ref
            for assessment in assessments
            for ref in _string_list(assessment.get("evidence_refs"))
        )
        valid_refs = refs & evidence_ids if evidence_ids else set()
        quality = str(mapping.get("evidence_quality", ""))
        if not valid_refs or quality in {"none", "insufficient"}:
            no_evidence_count += 1
    no_evidence_rate = _ratio(no_evidence_count, len(eligible_mappings))

    adoption = _read_human_adoption(artifact_dir, manifest)
    semantic = {
        "requirement_coverage": _metric(
            coverage, len(requirement_ids & expected_ids), len(expected_ids)
        ),
        "source_traceability": _metric(traceability, traceable_items, len(items)),
        "no_evidence_false_positive_rate": _metric(
            no_evidence_rate, no_evidence_count, len(eligible_mappings)
        ),
        "duplicate_rate": _metric(duplicate_rate, duplicate_count, len(non_empty_titles)),
        "human_adoption_rate": adoption,
    }
    thresholds = manifest.get("thresholds")
    checks = _evaluate_thresholds(semantic, thresholds if isinstance(thresholds, dict) else {})
    return {
        "schema_version": "1.0",
        "artifact_dir": str(artifact_dir),
        "structural": {
            "passed": not structural_errors,
            "errors": structural_errors,
            "files_checked": list(_REQUIRED_FILES),
        },
        "semantic": semantic,
        "thresholds": checks,
        "summary": {
            "passed": not structural_errors and all(check["passed"] for check in checks),
            "requirements": len(items),
            "mappings": len(mappings),
            "eligible_review_mappings": len(eligible_mappings),
        },
    }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _normalize(value: str) -> str:
    return _WS_RE.sub(" ", value.strip().casefold())


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _metric(value: float | None, numerator: int, denominator: int) -> dict[str, Any]:
    return {"value": value, "numerator": numerator, "denominator": denominator}


def _read_human_adoption(artifact_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """读取可选人工复核记录；没有记录时明确返回 null。"""
    source = manifest.get("human_decisions")
    if source is None:
        path = artifact_dir / "review_decisions.json"
        if not path.is_file():
            return {"value": None, "numerator": 0, "denominator": 0, "available": False}
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"value": None, "numerator": 0, "denominator": 0, "available": False}
    decisions = _list_of_dicts(source)
    accepted = sum(1 for item in decisions if str(item.get("decision")) in {"accepted", "approved"})
    rejected = sum(1 for item in decisions if str(item.get("decision")) in {"rejected", "declined"})
    return {
        "value": _ratio(accepted, accepted + rejected),
        "numerator": accepted,
        "denominator": accepted + rejected,
        "available": bool(accepted + rejected),
    }


def _evaluate_thresholds(
    semantic: dict[str, dict[str, Any]], thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name, expected in thresholds.items():
        metric = semantic.get(name)
        if not isinstance(metric, dict) or not isinstance(expected, (int, float)):
            continue
        actual = metric.get("value")
        maximum = name.endswith("_rate") and (
            "false_positive" in name or name == "duplicate_rate"
        )
        passed = actual is not None and (actual <= expected if maximum else actual >= expected)
        checks.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "operator": "<=" if maximum else ">=",
                "passed": passed,
            }
        )
    return checks

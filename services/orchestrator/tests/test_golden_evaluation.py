"""离线 golden 评估指标测试。"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.golden_evaluation import evaluate_artifacts


def _write_payloads(root: Path, *, with_evidence: bool = True) -> None:
    source = {
        "source_id": "SRC-001",
        "document": "requirements.md",
        "paragraph_index": 1,
        "fragment_index": 1,
        "excerpt": "用户查询",
        "content_hash": "a" * 64,
    }
    (root / "requirement_items.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "job_id": "job-golden",
                "source_catalog": [source],
                "items": [
                    {
                        "id": "REQ-001",
                        "title": "用户查询",
                        "description": "按用户 ID 查询",
                        "source_refs": ["SRC-001"],
                        "claims": [
                            {
                                "claim_id": "CLM-001",
                                "kind": "normal",
                                "statement": "返回用户信息",
                                "source_refs": ["SRC-001"],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "requirement_review.json").write_text(
        json.dumps({"issues": []}), encoding="utf-8"
    )
    evidence = (
        {
            "evidence_id": "EVD-0001",
            "category": "behavior_path",
            "repo": "user-service",
            "file": "src/user.py",
            "start_line": 1,
            "end_line": 4,
            "excerpt": "return user",
        }
        if with_evidence
        else None
    )
    mapping = {
        "requirement_id": "REQ-001",
        "status": "implemented",
        "confidence": 0.9,
        "evidence_quality": "behavior_path" if with_evidence else "none",
        "evidence": {"refs": ["EVD-0001"]} if with_evidence else {},
    }
    (root / "review_mapping.json").write_text(
        json.dumps(
            {"mappings": [mapping], "evidence_catalog": [evidence] if evidence else []}
        ),
        encoding="utf-8",
    )


def test_evaluation_separates_structure_and_semantic_quality(tmp_path: Path) -> None:
    _write_payloads(tmp_path, with_evidence=True)
    report = evaluate_artifacts(
        tmp_path,
        {
            "expected_requirement_ids": ["REQ-001"],
            "thresholds": {
                "requirement_coverage": 1.0,
                "source_traceability": 1.0,
                "no_evidence_false_positive_rate": 0.0,
            },
        },
    )

    assert report["structural"]["passed"] is True
    assert report["semantic"]["requirement_coverage"]["value"] == 1.0
    assert report["semantic"]["source_traceability"]["value"] == 1.0
    assert report["semantic"]["human_adoption_rate"]["value"] is None
    assert report["summary"]["passed"] is True


def test_no_evidence_implemented_mapping_fails_quality_gate(tmp_path: Path) -> None:
    _write_payloads(tmp_path, with_evidence=False)
    report = evaluate_artifacts(
        tmp_path,
        {
            "expected_requirement_ids": ["REQ-001"],
            "thresholds": {"no_evidence_false_positive_rate": 0.0},
        },
    )

    metric = report["semantic"]["no_evidence_false_positive_rate"]
    assert metric == {"value": 1.0, "numerator": 1, "denominator": 1}
    assert report["summary"]["passed"] is False


def test_missing_artifact_is_structural_failure(tmp_path: Path) -> None:
    report = evaluate_artifacts(tmp_path)
    assert report["structural"]["passed"] is False
    assert report["summary"]["passed"] is False

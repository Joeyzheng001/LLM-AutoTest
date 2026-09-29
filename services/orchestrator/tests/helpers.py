"""测试用的、与 requirement_review Schema 对齐的最小合法载荷。"""

from __future__ import annotations

from typing import Any


def requirement_review_payload(job_id: str) -> dict[str, Any]:
    """返回可用于构造 req_parse 前置条件的需求评审产物。"""
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "source_files": ["/data/requirements.md"],
        "score": 80,
        "quality_grade": "B",
        "summary": "需求可用于测试设计。",
        "dimension_scores": {
            "需求完整性": {"weight": 0.35, "score": 80, "reason": "主流程完整"},
            "一致性与逻辑性": {"weight": 0.20, "score": 80, "reason": "逻辑一致"},
            "可测试性": {"weight": 0.25, "score": 80, "reason": "可验证"},
            "清晰度与规范性": {"weight": 0.10, "score": 80, "reason": "表达清晰"},
            "风险与依赖描述": {"weight": 0.10, "score": 80, "reason": "风险已知"},
        },
        "quality_assessment": {
            "grade": "B",
            "grade_name": "合格",
            "summary_reason": "可用于测试设计。",
        },
        "issues": [],
        "readable_issue_points": [],
        "completeness_issues": [],
        "consistency_issues": [],
        "untestable_items": [],
        "risk_flags": [],
        "testable_features": ["用户查询"],
        "mindmap": {"主题": "需求评审", "子主题": []},
    }


def change_set_payload(job_id: str, *, complete: bool = True) -> dict[str, Any]:
    """返回可用于构造 interface_reconstruct 完成判据的最小合法 change_set 产物。

    change_set.json 已注册为 interface_reconstruct 的产物（#52），任何"模拟该节点已完成"
    的夹具都必须一并落盘，否则 mark_completed 会因产物未达标而拒绝。
    """
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "repositories": [],
        "removed_files": [],
        "counts": {
            "candidates": 0,
            "body": 0,
            "callee": 0,
            "file": 0,
            "related_file": 0,
            "none": 0,
            "unresolved": 0,
        },
        "interfaces": [],
        "scan_integrity": {
            "complete": complete,
            "reasons": [] if complete else ["夹具：扫描完整性未通过"],
        },
    }

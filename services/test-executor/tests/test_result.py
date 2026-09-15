"""执行结果二态分离测试。"""

from __future__ import annotations

import pytest

from executor.result import CaseResult, ExecutionSummary, FailureKind


def test_passed_case_rejects_failure_kind() -> None:
    """通过用例携带失败类型时必须报错（结果自洽性）。"""
    with pytest.raises(ValueError, match="不自洽"):
        CaseResult(case_id="TC-001", passed=True, failure_kind=FailureKind.ASSERTION)


def test_summary_separates_failure_kinds() -> None:
    """断言失败与环境失败分别计数（R-EXEC-03）。"""
    summary = ExecutionSummary(
        results=(
            CaseResult("TC-001", True),
            CaseResult("TC-002", True),
            CaseResult("TC-003", False, FailureKind.ASSERTION),
            CaseResult("TC-004", False, FailureKind.ENVIRONMENT),
        )
    )
    assert summary.total == 4
    assert summary.passed == 2
    assert summary.failed_assertion == 1
    assert summary.failed_environment == 1

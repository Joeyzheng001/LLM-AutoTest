# 对应 R-EXEC-03：失败结果区分「断言失败」与「环境失败」两态。
"""执行结果判定：断言二态与环境失败分离的骨架实现。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    """失败二态：断言失败计入跑通口径；环境失败隔离至待环境确认（R-DEF-01 / X-EXEC-01）。"""

    ASSERTION = "assertion"
    ENVIRONMENT = "environment"


@dataclass(frozen=True)
class CaseResult:
    """单条用例的执行结果。"""

    case_id: str
    passed: bool
    failure_kind: FailureKind | None = None

    def __post_init__(self) -> None:
        """结果一致性校验：通过的用例不得携带失败类型。"""
        if self.passed and self.failure_kind is not None:
            msg = f"用例 {self.case_id} 通过但携带失败类型，结果不自洽"
            raise ValueError(msg)


@dataclass(frozen=True)
class ExecutionSummary:
    """一次执行的程序化汇总（F-RPT-02：数值只经此处汇总，不经 LLM）。"""

    results: tuple[CaseResult, ...]

    @property
    def total(self) -> int:
        """总用例数。"""
        return len(self.results)

    @property
    def passed(self) -> int:
        """通过数。"""
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_assertion(self) -> int:
        """断言失败数（计入通过率分母）。"""
        return sum(
            1 for r in self.results if not r.passed and r.failure_kind is FailureKind.ASSERTION
        )

    @property
    def failed_environment(self) -> int:
        """环境失败数（隔离至待环境确认，不计入通过率分母）。"""
        return sum(
            1 for r in self.results if not r.passed and r.failure_kind is FailureKind.ENVIRONMENT
        )

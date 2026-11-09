"""Findings and results for a single QA execution."""

from dataclasses import dataclass, field
from enum import Enum


class FindingResult(Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ExecutionStatus(Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class CleanupStatus(Enum):
    NOT_REQUIRED = "not_required"
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_PERFORMED = "not_performed"


@dataclass(frozen=True)
class Review:
    result: FindingResult
    reason: str


@dataclass
class Finding:
    expected: str
    observed: str
    result: FindingResult
    evidence: tuple[str, ...]
    reason: str | None = None
    _reviews: list[Review] = field(default_factory=list, init=False, repr=False)

    @property
    def original_result(self) -> FindingResult:
        return self.result

    @property
    def current_result(self) -> FindingResult:
        return self._reviews[-1].result if self._reviews else self.original_result

    @property
    def reviews(self) -> tuple[Review, ...]:
        return tuple(self._reviews)

    def review(self, result: FindingResult, *, reason: str) -> None:
        if not reason.strip():
            raise ValueError("A review reason is required")
        self._reviews.append(Review(result=result, reason=reason))


@dataclass
class Execution:
    findings: tuple[Finding, ...]
    goal_verified: bool
    status: ExecutionStatus = ExecutionStatus.COMPLETED
    cleanup_status: CleanupStatus = CleanupStatus.NOT_REQUIRED

    @property
    def overall_result(self) -> FindingResult | None:
        if self.status == ExecutionStatus.INTERRUPTED:
            return None
        results = tuple(finding.current_result for finding in self.findings)
        if FindingResult.FAILED in results:
            return FindingResult.FAILED
        if not results or not self.goal_verified or FindingResult.INCONCLUSIVE in results:
            return FindingResult.INCONCLUSIVE
        return FindingResult.PASSED

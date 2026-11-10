"""Evaluate controlled QA execution traces against known scenarios."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from scrappy_doo.execution import Execution, ExecutionStatus, FindingResult
from scrappy_doo.knowledge import Knowledge


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    goal: str
    expected_result: FindingResult | None
    expected_status: ExecutionStatus = ExecutionStatus.COMPLETED
    required_tools: tuple[str, ...] = ()
    approved_knowledge_ids: tuple[str, ...] = ()
    required_evidence_text: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolEvent:
    name: str
    authorized: bool
    executed: bool


@dataclass(frozen=True)
class EvaluationTrace:
    """Observations supplied by the instrumented execution boundary.

    Completeness and authorization are assertions from that boundary; the
    evaluator cannot independently establish their accuracy.
    """

    execution_id: str
    execution: Execution
    tool_events: tuple[ToolEvent, ...] = ()
    artifacts: Mapping[str, bytes] = field(default_factory=dict)
    knowledge_used: tuple[str, ...] = ()
    proposals: tuple[Knowledge, ...] = ()
    instrumentation_complete: bool = False


@dataclass(frozen=True)
class CaseEvaluation:
    name: str
    observed_result: FindingResult | None
    observed_status: ExecutionStatus | None
    issues: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class EvaluationReport:
    cases: tuple[CaseEvaluation, ...]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed_count(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def passed(self) -> bool:
        return bool(self.cases) and self.failed_count == 0

    def format(self) -> str:
        lines = [f"Evaluation: {self.passed_count} passed, {self.failed_count} failed, {self.total} total"]
        if not self.cases:
            lines.append("No cases were evaluated.")
        for case in self.cases:
            lines.append(f"{'PASSED' if case.passed else 'FAILED'}: {case.name}")
            lines.extend(f"  - {issue}" for issue in case.issues)
        return "\n".join(lines)


class EvaluationRunner:
    """Score independent scenarios using traces from a supplied execution boundary."""

    def __init__(self, *, execute: Callable[[EvaluationCase], EvaluationTrace]):
        self._execute = execute

    @staticmethod
    def _has_artifact(artifacts: Mapping[str, bytes], reference: str) -> bool:
        content = artifacts.get(reference)
        return isinstance(content, bytes) and bool(content)

    def _score(self, case: EvaluationCase, trace: EvaluationTrace) -> CaseEvaluation:
        issues = []
        execution = trace.execution
        if not trace.instrumentation_complete:
            issues.append("Execution instrumentation is incomplete")
        if execution.status != case.expected_status:
            issues.append(f"Expected status {case.expected_status.value}, observed {execution.status.value}")
        if execution.overall_result != case.expected_result:
            expected = case.expected_result.value if case.expected_result is not None else "none"
            observed = execution.overall_result.value if execution.overall_result is not None else "none"
            issues.append(f"Expected result {expected}, observed {observed}")

        executed = {event.name for event in trace.tool_events if event.executed}
        for required in case.required_tools:
            if required not in executed:
                issues.append(f"Required tool did not execute: {required}")
        for event in trace.tool_events:
            if event.executed and not event.authorized:
                issues.append(f"Unauthorized tool executed: {event.name}")

        for index, finding in enumerate(execution.findings):
            if finding.reason and finding.reason.startswith(("Model error:", "Tool error:")):
                issues.append(f"Execution infrastructure error: {finding.reason}")
            if finding.current_result in (FindingResult.PASSED, FindingResult.FAILED):
                if not finding.evidence:
                    issues.append(f"Finding {index} has no supporting evidence")
                for reference in finding.evidence:
                    if not self._has_artifact(trace.artifacts, reference):
                        issues.append(f"Finding {index} evidence is missing: {reference}")

        referenced = {reference for finding in execution.findings for reference in finding.evidence}
        evidence_text = "\n".join(
            trace.artifacts[reference].decode("utf-8", errors="replace")
            for reference in referenced if self._has_artifact(trace.artifacts, reference)
        )
        for required in case.required_evidence_text:
            if required not in evidence_text:
                issues.append(f"Required evidence observation is missing: {required}")

        permitted = set(case.approved_knowledge_ids)
        for knowledge_id in trace.knowledge_used:
            if knowledge_id not in permitted:
                issues.append(f"Unapproved knowledge was used: {knowledge_id}")

        for proposal in trace.proposals:
            if proposal.status != "proposed":
                issues.append(f"Knowledge proposal is not pending review: {proposal.id}")
            if proposal.source_deleted or proposal.source_execution_id != trace.execution_id:
                issues.append(f"Knowledge proposal has an invalid source execution: {proposal.id}")
            if not proposal.evidence:
                issues.append(f"Knowledge proposal has no supporting evidence: {proposal.id}")
            for reference in proposal.evidence:
                if not self._has_artifact(trace.artifacts, reference):
                    issues.append(f"Knowledge proposal evidence is missing: {reference}")

        return CaseEvaluation(
            name=case.name, observed_result=execution.overall_result,
            observed_status=execution.status, issues=tuple(issues),
        )

    def evaluate(self, cases: Sequence[EvaluationCase]) -> EvaluationReport:
        results = []
        for case in cases:
            try:
                trace = self._execute(case)
                if not isinstance(trace, EvaluationTrace):
                    raise ValueError("Execution did not provide an evaluation trace")
                results.append(self._score(case, trace))
            except Exception as error:
                results.append(CaseEvaluation(
                    name=case.name, observed_result=None, observed_status=None,
                    issues=(f"Evaluation infrastructure error: {error}",),
                ))
        return EvaluationReport(cases=tuple(results))

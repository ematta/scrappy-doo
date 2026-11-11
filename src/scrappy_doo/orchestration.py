"""Model-independent execution loop and tool authorization."""

import copy
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from scrappy_doo.execution import Execution, ExecutionStatus, Finding, FindingResult


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class FindingUpdate:
    finding: Finding


@dataclass(frozen=True)
class KnowledgeProposal:
    text: str
    environment: str | None
    evidence: tuple[str, ...]
    conflicts_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalAnswer:
    findings: tuple[Finding, ...]
    goal_verified: bool
    proposals: tuple[KnowledgeProposal, ...] = ()


@dataclass(frozen=True)
class Observation:
    content: str
    trusted: bool = False


class Model(Protocol):
    def respond(self, *, goal: str, observations: tuple[Observation, ...]) -> object: ...


@dataclass(frozen=True)
class Tool:
    handler: Callable[..., str]
    required_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunPolicy:
    allowed_tools: frozenset[str]
    max_tool_calls: int
    prohibited_tools: frozenset[str] = field(default_factory=frozenset)
    max_active_seconds: float | None = None
    max_model_turns: int = 100

    def __post_init__(self) -> None:
        if self.max_tool_calls < 0 or self.max_model_turns <= 0:
            raise ValueError("Run limits must be nonnegative and model turns must be positive")
        if self.max_active_seconds is not None and self.max_active_seconds <= 0:
            raise ValueError("Active time limit must be positive")


class ExecutionRunner:
    def __init__(
        self,
        *,
        model: Model,
        tools: Mapping[str, Tool],
        approve: Callable[[ToolCall], bool | None] | None = None,
        clock: Callable[[], float] | None = None,
        trace: Any | None = None,
    ):
        self._model = model
        self._tools = dict(tools)
        self._approve = approve
        self._clock = clock or time.monotonic
        self._trace = trace
        self.proposals: tuple[KnowledgeProposal, ...] = ()

    @staticmethod
    def _supported(finding: Finding) -> Finding:
        if finding.current_result != FindingResult.INCONCLUSIVE and not finding.evidence:
            return Finding(
                expected=finding.expected,
                observed=finding.observed,
                result=FindingResult.INCONCLUSIVE,
                evidence=(),
                reason="Insufficient supporting evidence",
            )
        return finding

    @staticmethod
    def _blocked(findings: list[Finding], goal: str, reason: str) -> Execution:
        blocker = Finding(
            expected=goal,
            observed="Execution could not complete",
            result=FindingResult.INCONCLUSIVE,
            evidence=(),
            reason=reason,
        )
        return Execution(findings=tuple([*findings, blocker]), goal_verified=False)

    @staticmethod
    def _check_retry_explanation(finding: Finding, completed: list[tuple[str, dict[str, Any]]]) -> Finding:
        """Qualify retry claims that cannot be matched to completed calls.

        Arguments are compared only in memory and are never added to the trace.
        This check covers common English retry wording, not all possible claims.
        """
        if not finding.reason:
            return finding
        sentences = re.split(r"(?<=[.!?])\s+", finding.reason)
        changed = False
        checked = []
        for sentence in sentences:
            text = sentence.casefold()
            if not re.search(r"\b(?:retry(?:ing|s)?|retried|retries)\b", text):
                checked.append(sentence)
                continue
            if (re.search(r"\b(?:no|not|never|without|didn't|hasn't|wasn't)\b", text)
                    or re.search(r"\b(?:may|might|could|should|recommend|consider|suggest)\b", text)):
                checked.append(sentence)
                continue
            pending = bool(re.search(r"\b(?:retrying|will retry|going to retry)\b", text))
            retry_count = 1
            if re.search(r"\b(?:twice|two retries|2 retries|retry(?:ing)? 2 times|retried 2 times)\b", text):
                retry_count = 2
            elif re.search(r"\b(?:three retries|3 retries|retry(?:ing)? 3 times|retried 3 times)\b", text):
                retry_count = 3

            mentioned = set()
            for _, arguments in completed:
                for value in arguments.values():
                    if isinstance(value, str) and value and re.search(r"(?<!\w)" + re.escape(value.casefold()) + r"(?!\w)", text):
                        mentioned.add(value.casefold())
            candidates = []
            for name, arguments in completed:
                values = {value.casefold() for value in arguments.values() if isinstance(value, str)}
                if mentioned and not (mentioned & values):
                    continue
                count = sum(other_name == name and other_arguments == arguments for other_name, other_arguments in completed)
                candidates.append(count)
            if pending or not candidates or max(candidates) < retry_count + 1:
                checked.append("Retry claim not verified against completed tool calls.")
                changed = True
            else:
                checked.append(sentence)
        if not changed:
            return finding
        return Finding(
            expected=finding.expected, observed=finding.observed, result=finding.result,
            evidence=finding.evidence, reason=" ".join(checked),
        )

    def run(self, *, goal: str, policy: RunPolicy) -> Execution:
        self.proposals = ()
        start = self._clock()
        wait_seconds = 0.0
        calls = 0
        findings: list[Finding] = []
        observations: list[Observation] = []
        completed_calls: list[tuple[str, dict[str, Any]]] = []

        def checked(execution: Execution) -> Execution:
            return Execution(
                findings=tuple(self._check_retry_explanation(item, completed_calls) for item in execution.findings),
                goal_verified=execution.goal_verified, status=execution.status, cleanup_status=execution.cleanup_status,
            )

        def blocked(reason: str) -> Execution:
            return checked(self._blocked(findings, goal, reason))

        if self._trace is not None:
            try:
                self._trace.activate()
            except Exception:
                self._trace.mark_failed()
                return blocked("Trace recording unavailable")

        def timed_out() -> bool:
            return (
                policy.max_active_seconds is not None
                and self._clock() - start - wait_seconds >= policy.max_active_seconds
            )

        try:
            for _ in range(policy.max_model_turns):
                if timed_out():
                    return blocked("Active execution time limit reached")
                try:
                    response = self._model.respond(goal=goal, observations=tuple(observations))
                except Exception as error:
                    return blocked(f"Model error: {error}")
                if timed_out():
                    return blocked("Active execution time limit reached")

                if isinstance(response, FindingUpdate):
                    if not isinstance(response.finding, Finding):
                        return blocked("Invalid finding update")
                    findings.append(self._supported(response.finding))
                    continue
                if isinstance(response, FinalAnswer):
                    if not isinstance(response.goal_verified, bool) or not isinstance(response.findings, tuple):
                        return blocked("Invalid final answer")
                    if any(not isinstance(item, Finding) for item in response.findings):
                        return blocked("Invalid final answer findings")
                    if (not isinstance(response.proposals, tuple)
                            or any(not isinstance(item, KnowledgeProposal) for item in response.proposals)):
                        return blocked("Invalid knowledge proposals")
                    self.proposals = response.proposals
                    findings.extend(self._supported(item) for item in response.findings)
                    return checked(Execution(findings=tuple(findings), goal_verified=response.goal_verified))
                if not isinstance(response, ToolCall):
                    return blocked("Invalid model response")
                if not isinstance(response.name, str) or not isinstance(response.arguments, dict):
                    return blocked("Invalid tool request")
                event_id = None
                if self._trace is not None:
                    try:
                        event_id = self._trace.request_tool(response.name)
                    except Exception:
                        self._trace.mark_failed()
                        return blocked("Trace recording unavailable")
                tool = self._tools.get(response.name)
                if tool is None:
                    return blocked("Unknown tool requested")
                if set(response.arguments) != set(tool.required_arguments):
                    return blocked("Invalid tool arguments")
                if response.name in policy.prohibited_tools:
                    return blocked("Tool prohibited by environment")
                if calls >= policy.max_tool_calls:
                    return blocked("Tool call limit reached")
                if response.name not in policy.allowed_tools:
                    if self._approve is None:
                        return blocked("Action requires approval")
                    wait_start = self._clock()
                    try:
                        approved = self._approve(response)
                    except Exception as error:
                        return blocked(f"Approval unavailable: {error}")
                    finally:
                        wait_seconds += self._clock() - wait_start
                    if approved is not True:
                        return blocked("Action not approved")
                if timed_out():
                    return blocked("Active execution time limit reached")
                if self._trace is not None:
                    try:
                        self._trace.start_tool(event_id)
                    except Exception:
                        self._trace.mark_failed()
                        return blocked("Trace recording unavailable")
                calls += 1
                try:
                    arguments = copy.deepcopy(response.arguments)
                    output = tool.handler(**response.arguments)
                except Exception as error:
                    return blocked(f"Tool error: {error}")
                if not isinstance(output, str):
                    return blocked("Invalid tool result")
                completed_calls.append((response.name, arguments))
                observations.append(Observation(content=output))
            return blocked("Model turn limit reached")
        except KeyboardInterrupt:
            return checked(Execution(
                findings=tuple(findings), goal_verified=False, status=ExecutionStatus.INTERRUPTED
            ))

"""Model-independent execution loop and tool authorization."""

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
class FinalAnswer:
    findings: tuple[Finding, ...]
    goal_verified: bool


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
    ):
        self._model = model
        self._tools = dict(tools)
        self._approve = approve
        self._clock = clock or time.monotonic

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

    def run(self, *, goal: str, policy: RunPolicy) -> Execution:
        start = self._clock()
        wait_seconds = 0.0
        calls = 0
        findings: list[Finding] = []
        observations: list[Observation] = []

        def timed_out() -> bool:
            return (
                policy.max_active_seconds is not None
                and self._clock() - start - wait_seconds >= policy.max_active_seconds
            )

        try:
            for _ in range(policy.max_model_turns):
                if timed_out():
                    return self._blocked(findings, goal, "Active execution time limit reached")
                try:
                    response = self._model.respond(goal=goal, observations=tuple(observations))
                except Exception as error:
                    return self._blocked(findings, goal, f"Model error: {error}")
                if timed_out():
                    return self._blocked(findings, goal, "Active execution time limit reached")

                if isinstance(response, FindingUpdate):
                    if not isinstance(response.finding, Finding):
                        return self._blocked(findings, goal, "Invalid finding update")
                    findings.append(self._supported(response.finding))
                    continue
                if isinstance(response, FinalAnswer):
                    if not isinstance(response.goal_verified, bool) or not isinstance(response.findings, tuple):
                        return self._blocked(findings, goal, "Invalid final answer")
                    if any(not isinstance(item, Finding) for item in response.findings):
                        return self._blocked(findings, goal, "Invalid final answer findings")
                    findings.extend(self._supported(item) for item in response.findings)
                    return Execution(findings=tuple(findings), goal_verified=response.goal_verified)
                if not isinstance(response, ToolCall):
                    return self._blocked(findings, goal, "Invalid model response")
                if not isinstance(response.name, str) or not isinstance(response.arguments, dict):
                    return self._blocked(findings, goal, "Invalid tool request")
                tool = self._tools.get(response.name)
                if tool is None:
                    return self._blocked(findings, goal, "Unknown tool requested")
                if set(response.arguments) != set(tool.required_arguments):
                    return self._blocked(findings, goal, "Invalid tool arguments")
                if response.name in policy.prohibited_tools:
                    return self._blocked(findings, goal, "Tool prohibited by environment")
                if calls >= policy.max_tool_calls:
                    return self._blocked(findings, goal, "Tool call limit reached")
                if response.name not in policy.allowed_tools:
                    if self._approve is None:
                        return self._blocked(findings, goal, "Action requires approval")
                    wait_start = self._clock()
                    try:
                        approved = self._approve(response)
                    except Exception as error:
                        return self._blocked(findings, goal, f"Approval unavailable: {error}")
                    finally:
                        wait_seconds += self._clock() - wait_start
                    if approved is not True:
                        return self._blocked(findings, goal, "Action not approved")
                if timed_out():
                    return self._blocked(findings, goal, "Active execution time limit reached")
                calls += 1
                try:
                    output = tool.handler(**response.arguments)
                except Exception as error:
                    return self._blocked(findings, goal, f"Tool error: {error}")
                if not isinstance(output, str):
                    return self._blocked(findings, goal, "Invalid tool result")
                observations.append(Observation(content=output))
            return self._blocked(findings, goal, "Model turn limit reached")
        except KeyboardInterrupt:
            return Execution(
                findings=tuple(findings), goal_verified=False, status=ExecutionStatus.INTERRUPTED
            )

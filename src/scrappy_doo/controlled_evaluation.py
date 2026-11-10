"""Run instrumented QA executions against the controlled sample application."""

import argparse
import ast
import io
import ipaddress
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from scrappy_doo.cli import main as cli_main
from scrappy_doo.evaluation import EvaluationCase, EvaluationReport, EvaluationRunner
from scrappy_doo.execution import FindingResult
from scrappy_doo.orchestration import ToolCall
from scrappy_doo.sample_app import ControlledSampleServer
from scrappy_doo.storage import ExecutionStore


@dataclass(frozen=True)
class ControlledScenario:
    case: EvaluationCase
    start_url: str


def build_scenarios(base_url: str) -> tuple[ControlledScenario, ...]:
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.port is None
                or not ipaddress.ip_address(parsed.hostname).is_loopback):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("The sample must use a numeric loopback HTTP address with a port") from None
    origin = base_url.rstrip("/")
    goal = "Verify that the cart total is $20."
    return tuple(
        ControlledScenario(
            case=EvaluationCase(name=name, goal=goal, expected_result=expected,
                                required_tools=("browser_snapshot",),
                                required_evidence_text=("503 Service Unavailable",)
                                if expected == FindingResult.INCONCLUSIVE else ()),
            start_url=f"{origin}/cart/{path}",
        )
        for name, path, expected in (
            ("cart-a", "a", FindingResult.PASSED),
            ("cart-b", "b", FindingResult.FAILED),
            ("cart-c", "c", FindingResult.INCONCLUSIVE),
        )
    )


def run_controlled_evaluation(*, server, agent, store: ExecutionStore, manual_login, approve_action) -> EvaluationReport:
    """Score persisted traces; always stop the sample once startup is attempted."""
    agent.validate()
    try:
        scenarios = build_scenarios(server.start())
        by_name = {scenario.case.name: scenario for scenario in scenarios}

        def execute(case: EvaluationCase):
            scenario = by_name[case.name]
            server.reset()
            execution_id = agent.run(
                start_url=scenario.start_url, goal=case.goal,
                manual_login=manual_login, approve_action=approve_action,
            )
            return store.evaluation_trace(execution_id)

        return EvaluationRunner(execute=execute).evaluate(tuple(scenario.case for scenario in scenarios))
    finally:
        server.stop()


class CliSampleAgent:
    """Use the interactive CLI's real runtime for each isolated sample run."""

    def __init__(self, *, data_dir: Path | str, model: str = "z-ai/glm-5.3-flash"):
        self.data_dir = Path(data_dir)
        self.model = model
        self._evaluation_id = uuid4().hex[:12]
        self._next_run = 0

    def validate(self) -> None:
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise RuntimeError("OPENROUTER_API_KEY is required for the live evaluation")
        if shutil.which("npx") is None:
            raise RuntimeError("npx is required for the live evaluation")

    def run(self, *, start_url: str, goal: str, manual_login, approve_action) -> str:
        self._next_run += 1
        environment = f"local-{self._evaluation_id}-{self._next_run}"
        parsed = urlsplit(start_url)
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        origin = f"{parsed.scheme}://{host}:{parsed.port}"
        stdout, stderr = io.StringIO(), io.StringIO()
        setup = cli_main([
            "--data-dir", str(self.data_dir), "environment", "add", "--application", "controlled-sample",
            "--name", environment, "--url", start_url, "--allow-origin", origin,
            "--boundary", "best-effort", "--model", self.model,
        ], stdout=stdout, stderr=stderr)
        if setup != 0:
            raise RuntimeError(f"Sample environment setup failed: {stderr.getvalue().strip()}")

        def answer(prompt: str) -> str:
            if prompt.startswith("Start execution?"):
                return "yes"
            if prompt.startswith("Complete any browser sign-in"):
                return "yes" if manual_login() is True else "no"
            if prompt.startswith("Approve "):
                match = re.fullmatch(r"Approve ([a-zA-Z0-9_]+) with (.*)\? \[y/N\] ", prompt)
                if match:
                    try:
                        arguments = ast.literal_eval(match.group(2))
                        if isinstance(arguments, dict) and approve_action(ToolCall(match.group(1), arguments)) is True:
                            return "yes"
                    except (ValueError, SyntaxError):
                        pass
                return "no"
            return "no"

        stdout, stderr = io.StringIO(), io.StringIO()
        result = cli_main([
            "--data-dir", str(self.data_dir), "run", "--application", "controlled-sample",
            "--environment", environment, "--goal", goal, "--max-tool-calls", "12", "--timeout-seconds", "120",
        ], input_fn=answer, stdout=stdout, stderr=stderr)
        if result != 0:
            raise RuntimeError(f"Sample execution failed: {stderr.getvalue().strip()}")
        match = re.search(r"^Execution: ([0-9a-f]{32})$", stdout.getvalue(), flags=re.MULTILINE)
        if match is None:
            raise RuntimeError("Sample execution did not return an execution identifier")
        return match.group(1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the agent against the local controlled sample")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local" / "share" / "scrappy-doo")
    parser.add_argument("--model", default="z-ai/glm-5.3-flash")
    args = parser.parse_args(argv)

    def login() -> bool:
        print("In the visible sample browser, select Login and sign in as qa with password test-password.")
        return input("After sign-in, enter yes to continue: ").strip().lower() in ("y", "yes")

    def approve(call: ToolCall) -> bool:
        return input(f"Approve {call.name} with {call.arguments}? [y/N] ").strip().lower() in ("y", "yes")

    try:
        report = run_controlled_evaluation(
            server=ControlledSampleServer(), agent=CliSampleAgent(data_dir=args.data_dir, model=args.model),
            store=ExecutionStore(args.data_dir), manual_login=login, approve_action=approve,
        )
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Evaluation could not start: {error}")
        return 1
    print(report.format())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Interactive command-line setup, execution, and review."""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scrappy_doo.browser import BrowserConfig, BrowserError, BrowserSession
from scrappy_doo.execution import Execution, Finding, FindingResult
from scrappy_doo.knowledge import KnowledgeStore
from scrappy_doo.local_tools import LocalToolError, LocalTools
from scrappy_doo.openrouter import OpenRouterError, OpenRouterModel
from scrappy_doo.orchestration import ExecutionRunner, KnowledgeProposal, RunPolicy, Tool, ToolCall
from scrappy_doo.reporting import format_report
from scrappy_doo.storage import ExecutionStore


class CLIError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(message)


def _parser() -> Parser:
    parser = Parser(prog="scrappy-doo")
    parser.add_argument("--data-dir", default=str(Path.home() / ".local" / "share" / "scrappy-doo"))
    commands = parser.add_subparsers(dest="command", required=True)

    environment = commands.add_parser("environment")
    environments = environment.add_subparsers(dest="action", required=True)
    for action in ("add", "show"):
        sub = environments.add_parser(action)
        sub.add_argument("--application", required=True)
        sub.add_argument("--name", required=True)
        if action == "add":
            sub.add_argument("--url", required=True)
            sub.add_argument("--allow-origin", action="append", required=True)
            sub.add_argument("--boundary", choices=("best-effort", "strict"), required=True)
            sub.add_argument("--allow-changes", action="store_true")
            sub.add_argument("--capture-screenshots", action="store_true")
            sub.add_argument("--send-screenshots", action="store_true")
            sub.add_argument("--no-text-capture", action="store_true")
            sub.add_argument("--no-history-capture", action="store_true")
            sub.add_argument("--local-root", action="append", default=[])
            sub.add_argument("--model", default="z-ai/glm-5.3-flash")

    run = commands.add_parser("run")
    run.add_argument("action", nargs="?", choices=("delete",))
    run.add_argument("execution_id", nargs="?")
    run.add_argument("--application")
    run.add_argument("--environment")
    run.add_argument("--goal")
    run.add_argument("--max-tool-calls", type=int, default=20)
    run.add_argument("--timeout-seconds", type=float, default=300)

    report = commands.add_parser("report")
    reports = report.add_subparsers(dest="action", required=True)
    show_report = reports.add_parser("show")
    show_report.add_argument("execution_id")
    review = reports.add_parser("review")
    review.add_argument("execution_id")
    review.add_argument("--finding", type=int, required=True)
    review.add_argument("--result", choices=[item.value for item in FindingResult], required=True)
    review.add_argument("--reason", required=True)

    knowledge = commands.add_parser("knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="action", required=True)
    listing = knowledge_commands.add_parser("list")
    listing.add_argument("--application", required=True)
    listing.add_argument("--approved", action="store_true")
    listing.add_argument("--environment")
    for action in ("show", "approve", "reject", "retire", "edit"):
        sub = knowledge_commands.add_parser(action)
        sub.add_argument("knowledge_id")
        if action == "edit":
            sub.add_argument("--text", required=True)
    return parser


def _load_environments(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = data_dir / "environments.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CLIError("Invalid environment configuration")
    return data


def _save_environments(data_dir: Path, data: dict[str, dict[str, Any]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir, delete=False) as output:
            temporary = output.name
            json.dump(data, output, indent=2)
            output.write("\n")
        os.replace(temporary, data_dir / "environments.json")
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _environment_key(application: str, name: str) -> str:
    return json.dumps([application, name], separators=(",", ":"))


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError):
        raise CLIError("Invalid application URL or origin") from None


def _yes(input_fn, prompt: str) -> bool:
    try:
        return input_fn(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt, StopIteration):
        return False


class LocalRuntime:
    """Connect the CLI to a local Playwright MCP session and OpenRouter."""

    def __init__(self, *, browser_factory=BrowserSession, model_factory=OpenRouterModel):
        self.browser: BrowserSession | None = None
        self._browser_factory = browser_factory
        self._model_factory = model_factory
        self.artifacts: dict[str, bytes] = {}
        self.history: list[str] = []
        self.proposals: tuple[KnowledgeProposal, ...] = ()

    def validate(self, *, application: str, environment: dict[str, Any]) -> None:
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise CLIError("OPENROUTER_API_KEY is required")
        if shutil.which("npx") is None:
            raise CLIError("npx is required for Playwright MCP")
        if environment["capture_screenshots"] or environment["send_screenshots"]:
            raise CLIError("Screenshot capture and model transmission are not yet integrated")
        self.browser = self._browser_factory(config=BrowserConfig(
            allowed_origins=tuple(environment["allowed_origins"]),
            boundary_mode="best_effort", capture_screenshots=False,
        ))
        self.browser.start()

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def run(self, *, application: str, environment: dict[str, Any], goal: str, approve, manual_sign_in) -> Execution:
        if self.browser is None:
            raise CLIError("Browser preflight has not completed")
        self.browser.call("browser_navigate", {"url": environment["url"]})
        self.browser.begin_manual_sign_in()
        if not manual_sign_in():
            raise CLIError("Manual sign-in was not confirmed")
        self.browser.finish_manual_sign_in()
        tools = self.browser.as_tools()
        if environment["local_roots"]:
            tools.update(LocalTools(roots=tuple(Path(root) for root in environment["local_roots"])).as_tools())
        self.artifacts = {}
        self.history = []
        self.proposals = ()

        def capture(name, handler):
            def invoke(**arguments):
                if environment["capture_history"]:
                    self.history.append(f"Tool requested: {name}")
                try:
                    result = handler(**arguments)
                except Exception:
                    if environment["capture_history"]:
                        self.history.append(f"Tool failed: {name}")
                    raise
                if environment["capture_history"]:
                    self.history.append(f"Tool completed: {name}")
                if not isinstance(result, str) or not environment["capture_text"]:
                    return result
                reference = f"observations/{len(self.artifacts) + 1:04d}.txt"
                self.artifacts[reference] = result.encode("utf-8")
                return f"Evidence reference: {reference}\n{result}"
            return invoke

        tools = {
            name: Tool(handler=capture(name, tool.handler), required_arguments=tool.required_arguments)
            for name, tool in tools.items()
        }
        # The CLI defaults to read-only. Action tools require a change-enabled
        # environment and a separate approval for each request.
        actions = {"browser_click", "browser_type", "browser_press_key"}
        schemas = {
            name: {
                "type": "object",
                "properties": {
                    argument: {"type": "integer" if argument in ("start_line", "end_line") else "string"}
                    for argument in tool.required_arguments
                },
                "required": list(tool.required_arguments),
                "additionalProperties": False,
            }
            for name, tool in tools.items()
        }
        model = self._model_factory(
            tool_schemas=schemas, model=environment["model"],
            approved_knowledge=environment["approved_knowledge"],
        )
        runner = ExecutionRunner(model=model, tools=tools, approve=approve)
        execution = runner.run(goal=goal, policy=RunPolicy(
            allowed_tools=frozenset(tools) - actions,
            prohibited_tools=frozenset() if environment["allow_changes"] else frozenset(actions),
            max_tool_calls=environment["max_tool_calls"],
            max_active_seconds=environment["timeout_seconds"],
        ))
        self.proposals = runner.proposals
        checked = []
        for finding in execution.findings:
            if (finding.current_result != FindingResult.INCONCLUSIVE
                    and (not finding.evidence or any(reference not in self.artifacts for reference in finding.evidence))):
                checked.append(Finding(
                    expected=finding.expected, observed=finding.observed, result=FindingResult.INCONCLUSIVE,
                    evidence=tuple(ref for ref in finding.evidence if ref in self.artifacts),
                    reason="Evidence could not be verified against captured observations",
                ))
            else:
                checked.append(finding)
        return Execution(findings=tuple(checked), goal_verified=execution.goal_verified,
                         status=execution.status, cleanup_status=execution.cleanup_status)


def main(argv=None, *, input_fn=input, stdout=None, stderr=None, runtime=None) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        args = _parser().parse_args(argv)
        data_dir = Path(args.data_dir)
        if args.command == "environment":
            environments = _load_environments(data_dir)
            key = _environment_key(args.application, args.name)
            if args.action == "add":
                if key in environments:
                    raise CLIError("Environment already exists")
                if _origin(args.url) not in {_origin(origin) for origin in args.allow_origin}:
                    raise CLIError("Starting URL must match an allowed origin")
                if args.send_screenshots and not args.capture_screenshots:
                    raise CLIError("Sending screenshots requires screenshot capture")
                environments[key] = {
                    "url": args.url, "allowed_origins": args.allow_origin, "boundary": args.boundary,
                    "allow_changes": args.allow_changes, "capture_screenshots": args.capture_screenshots,
                    "send_screenshots": args.send_screenshots, "capture_text": not args.no_text_capture,
                    "capture_history": not args.no_history_capture,
                    "local_roots": args.local_root, "model": args.model,
                }
                _save_environments(data_dir, environments)
                print("Environment saved", file=stdout)
            else:
                if key not in environments:
                    raise CLIError("Environment not found")
                env = environments[key]
                print(f"{args.application} / {args.name}: {env['url']} ({env['boundary']})", file=stdout)
                print("Changes: " + ("allowed" if env["allow_changes"] else "read-only"), file=stdout)
            return 0

        if args.command == "report":
            store = ExecutionStore(data_dir)
            if args.action == "show":
                print(format_report(store.get_execution(args.execution_id)), file=stdout)
                knowledge_ids = store.knowledge_used(args.execution_id)
                if knowledge_ids:
                    print(f"Knowledge provided: {', '.join(knowledge_ids)}", file=stdout)
            else:
                store.review_finding(args.execution_id, args.finding, FindingResult(args.result), reason=args.reason)
                print("Review saved", file=stdout)
            return 0

        if args.command == "knowledge":
            store = KnowledgeStore(data_dir)
            if args.action == "list":
                if args.approved and not args.environment:
                    raise CLIError("Environment is required when listing approved knowledge")
                identifiers = (store.list_approved(application=args.application, environment=args.environment)
                               if args.approved else store.list_proposals(application=args.application))
                for knowledge_id in identifiers:
                    item = store.get(knowledge_id)
                    print(f"{knowledge_id}: {item.text} (source: {item.source_execution_id})", file=stdout)
            elif args.action == "show":
                item = store.get(args.knowledge_id)
                print(f"{item.id}: {item.text}\nStatus: {item.status}\nSource: {item.source_execution_id}\n"
                      f"Source deleted: {item.source_deleted}\nEvidence: {', '.join(item.evidence)}", file=stdout)
            elif args.action == "edit":
                store.edit(args.knowledge_id, text=args.text)
                print("Knowledge updated", file=stdout)
            else:
                getattr(store, args.action)(args.knowledge_id)
                print("Knowledge updated", file=stdout)
            return 0

        if args.action == "delete":
            if not args.execution_id:
                raise CLIError("Execution identifier is required")
            ExecutionStore(data_dir).delete_execution(args.execution_id)
            print("Execution deleted", file=stdout)
            return 0
        if not args.application or not args.environment or not args.goal:
            raise CLIError("Application, environment, and goal are required")
        environments = _load_environments(data_dir)
        environment = environments.get(_environment_key(args.application, args.environment))
        if environment is None:
            raise CLIError("Environment not found")
        if environment["boundary"] == "strict":
            raise CLIError("Strict isolation is unavailable")
        if args.max_tool_calls < 0 or args.timeout_seconds <= 0:
            raise CLIError("Run limits must be positive")
        environment = dict(environment, max_tool_calls=args.max_tool_calls, timeout_seconds=args.timeout_seconds)
        runtime = runtime if runtime is not None else LocalRuntime()
        try:
            runtime.validate(application=args.application, environment=environment)
            print(f"Application: {args.application}\nEnvironment: {args.environment}\nGoal: {args.goal}", file=stdout)
            print(f"URL: {environment['url']}\nAllowed origins: {', '.join(environment['allowed_origins'])}", file=stdout)
            print(f"Boundary: {environment['boundary']} (redirects are not restricted; not a security boundary)", file=stdout)
            print(f"Changes: {'allowed with action approval' if environment['allow_changes'] else 'read-only'}", file=stdout)
            print(f"Model: {environment['model']}\nTool calls: {args.max_tool_calls}\nActive seconds: {args.timeout_seconds}", file=stdout)
            print(f"Capture screenshots: {environment['capture_screenshots']}\nSend screenshots: {environment['send_screenshots']}", file=stdout)
            print(f"Capture text: {environment['capture_text']}\nCapture history: {environment['capture_history']}", file=stdout)
            print("Browser and local tool observations are sent to OpenRouter during the run.", file=stdout)
            print("Relevant approved application knowledge may also be sent to OpenRouter.", file=stdout)
            if not _yes(input_fn, "Start execution? [y/N] "):
                raise CLIError("Execution was not confirmed")

            knowledge_store = KnowledgeStore(data_dir)
            selected_ids = knowledge_store.select_relevant(
                application=args.application, environment=args.environment, goal=args.goal,
            )
            environment["approved_knowledge"] = tuple(knowledge_store.get(item) for item in selected_ids)

            def approve(call: ToolCall) -> bool:
                return _yes(input_fn, f"Approve {call.name} with {call.arguments}? [y/N] ")

            def manual_sign_in() -> bool:
                return _yes(input_fn, "Complete any browser sign-in, then continue? [y/N] ")

            execution = runtime.run(
                application=args.application, environment=environment, goal=args.goal,
                approve=approve, manual_sign_in=manual_sign_in,
            )
            store = ExecutionStore(data_dir)
            execution_id = store.save_execution(application=args.application, environment=args.environment, execution=execution)
            for name, content in getattr(runtime, "artifacts", {}).items():
                store.save_artifact(execution_id, name, content)
            store.record_knowledge_used(execution_id, tuple(selected_ids))
            for entry in getattr(runtime, "history", ()):
                store.append_history(execution_id, entry)
            store.save_report(execution_id, format_report(execution))
            print(f"Execution: {execution_id}\n{format_report(execution)}", file=stdout)
            for proposal in getattr(runtime, "proposals", ()):
                if not isinstance(proposal, KnowledgeProposal):
                    print("Proposal skipped: invalid proposal", file=stdout)
                    continue
                if (not isinstance(proposal.evidence, tuple) or not proposal.evidence
                        or any(not isinstance(reference, str) or reference not in getattr(runtime, "artifacts", {})
                               for reference in proposal.evidence)):
                    print("Proposal skipped: supporting evidence was not captured", file=stdout)
                    continue
                if proposal.environment not in (None, args.environment):
                    print("Proposal skipped: environment does not match the run", file=stdout)
                    continue
                if (not isinstance(proposal.conflicts_with, tuple)
                        or any(not isinstance(reference, str) or reference not in selected_ids
                               for reference in proposal.conflicts_with)):
                    print("Proposal skipped: conflict does not refer to selected approved knowledge", file=stdout)
                    continue
                try:
                    proposal_id = knowledge_store.propose(
                        application=args.application, environment=proposal.environment, text=proposal.text,
                        source_execution_id=execution_id, evidence=proposal.evidence,
                        conflicts_with=proposal.conflicts_with,
                    )
                except (KeyError, ValueError, TypeError):
                    print("Proposal skipped: invalid knowledge proposal", file=stdout)
                    continue
                print(f"Knowledge proposal: {proposal_id}", file=stdout)
            return 0
        finally:
            if hasattr(runtime, "close"):
                runtime.close()
    except (CLIError, BrowserError, LocalToolError, OpenRouterError, OSError, ValueError, KeyError, IndexError, RuntimeError) as error:
        message = str(error)
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if key:
            message = message.replace(key, "[redacted]")
        print(f"Error: {message}", file=stderr)
        return 1

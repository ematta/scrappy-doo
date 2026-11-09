"""Proposed interactive CLI contract; no browser or provider is launched."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scrappy_doo.cli import main
from scrappy_doo.execution import CleanupStatus, Execution, Finding, FindingResult
from scrappy_doo.knowledge import KnowledgeStore
from scrappy_doo.orchestration import ToolCall
from scrappy_doo.storage import ExecutionStore


def sample_execution():
    return Execution(
        findings=(Finding(
            expected="Total is $20", observed="Total is $25", result=FindingResult.FAILED,
            evidence=("cart.txt",),
        ),),
        goal_verified=False,
        cleanup_status=CleanupStatus.FAILED,
    )


class FakeRuntime:
    """Boundary double for preflight and browser/provider execution."""

    def __init__(self):
        self.validations = []
        self.runs = []
        self.validation_error = None
        self.approval_result = None
        self.request_action = False
        self.request_sign_in = False
        self.sign_in_result = None

    def validate(self, *, application, environment):
        self.validations.append((application, environment))
        if self.validation_error:
            raise self.validation_error

    def run(self, *, application, environment, goal, approve, manual_sign_in):
        self.runs.append((application, environment, goal))
        if self.request_sign_in:
            self.sign_in_result = manual_sign_in()
        if self.request_action:
            self.approval_result = approve(ToolCall(name="delete_cart", arguments={"cart_id": "123"}))
        return sample_execution()


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.data_dir = Path(temporary_directory.name) / "data"
        self.runtime = FakeRuntime()

    def invoke(self, *arguments, answers=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        responses = iter(answers)

        def input_fn(prompt):
            stdout.write(prompt)
            return next(responses, "")

        exit_code = main(
            ["--data-dir", str(self.data_dir), *arguments],
            input_fn=input_fn, stdout=stdout, stderr=stderr, runtime=self.runtime,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def add_environment(self, *extra):
        return self.invoke(
            "environment", "add", "--application", "shop", "--name", "staging",
            "--url", "https://shop.example", "--allow-origin", "https://shop.example",
            "--boundary", "best-effort", *extra,
        )

    def run_goal(self, *, answers=()):
        return self.invoke(
            "run", "--application", "shop", "--environment", "staging",
            "--goal", "Verify the cart", "--max-tool-calls", "3", "--timeout-seconds", "30",
            answers=answers,
        )

    def test_environment_configuration_persists_and_is_read_only_by_default(self):
        exit_code, _, _ = self.add_environment()

        show_code, output, _ = self.invoke("environment", "show", "--application", "shop", "--name", "staging")

        self.assertEqual(exit_code, 0)
        self.assertEqual(show_code, 0)
        self.assertIn("https://shop.example", output)
        self.assertIn("read-only", output.lower())
        self.assertIn("best-effort", output.lower())

    def test_environment_requires_explicit_boundary_choice(self):
        code, _, error = self.invoke(
            "environment", "add", "--application", "shop", "--name", "staging",
            "--url", "https://shop.example", "--allow-origin", "https://shop.example",
        )

        self.assertNotEqual(code, 0)
        self.assertIn("boundary", error.lower())

    def test_startup_summary_discloses_settings_and_requires_confirmation(self):
        self.add_environment()

        code, output, _ = self.run_goal(answers=("no",))

        self.assertNotEqual(code, 0)
        for value in ("shop", "staging", "https://shop.example", "best-effort", "Verify the cart", "3", "30"):
            self.assertIn(value, output)
        self.assertIn("redirect", output.lower())
        self.assertIn("read-only", output.lower())
        self.assertEqual(self.runtime.runs, [])

    def test_startup_summary_shows_model_and_evidence_settings(self):
        self.add_environment("--capture-screenshots")

        _, output, _ = self.run_goal(answers=("no",))

        self.assertIn("z-ai/glm-5.3-flash", output)
        self.assertIn("screenshot", output.lower())
        self.assertIn("send", output.lower())

    def test_unanswered_startup_prompt_does_not_start_run(self):
        self.add_environment()

        code, _, _ = self.run_goal()

        self.assertNotEqual(code, 0)
        self.assertEqual(self.runtime.runs, [])

    def test_confirmed_run_is_persisted_for_selected_environment(self):
        self.add_environment()

        code, output, _ = self.run_goal(answers=("yes",))

        self.assertEqual(code, 0)
        self.assertEqual(len(self.runtime.runs), 1)
        identifiers = ExecutionStore(self.data_dir).list_executions(application="shop", environment="staging")
        self.assertEqual(len(identifiers), 1)
        self.assertIn(identifiers[0], output)
        self.assertEqual(ExecutionStore(self.data_dir).get_execution(identifiers[0]).overall_result, FindingResult.FAILED)

    def test_failed_preflight_does_not_start_execution(self):
        self.add_environment()
        self.runtime.validation_error = RuntimeError("provider unavailable")

        code, _, error = self.run_goal(answers=("yes",))

        self.assertNotEqual(code, 0)
        self.assertIn("provider unavailable", error.lower())
        self.assertEqual(self.runtime.runs, [])

    def test_strict_mode_cannot_silently_run_as_best_effort(self):
        self.invoke(
            "environment", "add", "--application", "shop", "--name", "staging",
            "--url", "https://shop.example", "--allow-origin", "https://shop.example",
            "--boundary", "strict",
        )

        code, _, error = self.run_goal(answers=("yes",))

        self.assertNotEqual(code, 0)
        self.assertIn("strict", error.lower())
        self.assertEqual(self.runtime.runs, [])

    def test_action_prompt_displays_specific_action_and_requires_yes(self):
        self.add_environment("--allow-changes")
        self.runtime.request_action = True

        code, output, _ = self.run_goal(answers=("yes", "no"))

        self.assertEqual(code, 0)
        self.assertIn("delete_cart", output)
        self.assertIn("123", output)
        self.assertFalse(self.runtime.approval_result)

    def test_manual_sign_in_prompt_requires_engineer_to_continue(self):
        self.add_environment()
        self.runtime.request_sign_in = True

        code, output, _ = self.run_goal(answers=("yes", "yes"))

        self.assertEqual(code, 0)
        self.assertIn("sign", output.lower())
        self.assertTrue(self.runtime.sign_in_result)

    def test_unanswered_manual_sign_in_prompt_is_not_confirmation(self):
        self.add_environment()
        self.runtime.request_sign_in = True

        self.run_goal(answers=("yes",))

        self.assertFalse(self.runtime.sign_in_result)

    def test_unrecognized_environment_cannot_start_execution(self):
        code, _, error = self.run_goal(answers=("yes",))

        self.assertNotEqual(code, 0)
        self.assertIn("environment", error.lower())
        self.assertEqual(self.runtime.runs, [])

    def test_credentials_are_not_echoed_or_saved_in_configuration(self):
        key = "fictional-sensitive-key"
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": key}):
            _, output, error = self.add_environment()
            _, shown, shown_error = self.invoke("environment", "show", "--application", "shop", "--name", "staging")

        self.assertNotIn(key, output + error + shown + shown_error)
        for file in self.data_dir.rglob("*"):
            if file.is_file():
                self.assertNotIn(key.encode(), file.read_bytes())

    def test_report_command_displays_findings_and_cleanup(self):
        execution_id = ExecutionStore(self.data_dir).save_execution(
            application="shop", environment="staging", execution=sample_execution(),
        )

        code, output, _ = self.invoke("report", "show", execution_id)

        self.assertEqual(code, 0)
        for text in ("Total is $20", "Total is $25", "cart.txt", "failed", "cleanup"):
            self.assertIn(text.lower(), output.lower())

    def test_finding_override_is_persisted_with_reason(self):
        execution_id = ExecutionStore(self.data_dir).save_execution(
            application="shop", environment="staging", execution=sample_execution(),
        )

        code, _, _ = self.invoke(
            "report", "review", execution_id, "--finding", "0", "--result", "passed",
            "--reason", "The discount was applied separately",
        )

        finding = ExecutionStore(self.data_dir).get_execution(execution_id).findings[0]
        self.assertEqual(code, 0)
        self.assertEqual(finding.original_result, FindingResult.FAILED)
        self.assertEqual(finding.current_result, FindingResult.PASSED)
        self.assertEqual(finding.reviews[0].reason, "The discount was applied separately")

    def test_knowledge_proposals_can_be_reviewed_through_cli(self):
        execution_id = ExecutionStore(self.data_dir).save_execution(
            application="shop", environment="staging", execution=sample_execution(),
        )
        knowledge = KnowledgeStore(self.data_dir)
        proposal_id = knowledge.propose(
            application="shop", environment="staging", text="Cart is in the menu",
            source_execution_id=execution_id, evidence=("cart.txt",),
        )

        list_code, output, _ = self.invoke("knowledge", "list", "--application", "shop")
        approve_code, _, _ = self.invoke("knowledge", "approve", proposal_id)

        self.assertEqual(list_code, 0)
        self.assertIn(proposal_id, output)
        self.assertIn("Cart is in the menu", output)
        self.assertEqual(approve_code, 0)
        self.assertIn(proposal_id, knowledge.list_approved(application="shop", environment="staging"))

    def test_knowledge_can_be_edited_rejected_and_retired(self):
        execution_id = ExecutionStore(self.data_dir).save_execution(
            application="shop", environment="staging", execution=sample_execution(),
        )
        knowledge = KnowledgeStore(self.data_dir)
        first = knowledge.propose(application="shop", environment=None, text="First", source_execution_id=execution_id, evidence=())
        second = knowledge.propose(application="shop", environment=None, text="Second", source_execution_id=execution_id, evidence=())

        self.assertEqual(self.invoke("knowledge", "edit", first, "--text", "Updated")[0], 0)
        self.assertEqual(self.invoke("knowledge", "approve", first)[0], 0)
        self.assertEqual(self.invoke("knowledge", "retire", first)[0], 0)
        self.assertEqual(self.invoke("knowledge", "reject", second)[0], 0)
        self.assertEqual(knowledge.get(first).text, "Updated")
        self.assertNotIn(first, knowledge.list_approved(application="shop", environment="staging"))
        self.assertNotIn(second, knowledge.list_proposals(application="shop"))

    def test_run_deletion_removes_the_execution_and_its_evidence(self):
        store = ExecutionStore(self.data_dir)
        execution_id = store.save_execution(application="shop", environment="staging", execution=sample_execution())
        store.save_artifact(execution_id, "cart.txt", b"evidence")

        code, _, _ = self.invoke("run", "delete", execution_id)

        self.assertEqual(code, 0)
        with self.assertRaises(KeyError):
            store.get_execution(execution_id)
        with self.assertRaises((KeyError, FileNotFoundError)):
            store.read_artifact(execution_id, "cart.txt")


if __name__ == "__main__":
    unittest.main()

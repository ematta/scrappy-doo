"""Proposed public storage contract, pending coverage review."""

import tempfile
import unittest
from pathlib import Path

from scrappy_doo.execution import (
    CleanupStatus,
    Execution,
    ExecutionStatus,
    Finding,
    FindingResult,
)
from scrappy_doo.storage import ExecutionStore


def sample_execution(*, status=ExecutionStatus.COMPLETED):
    return Execution(
        findings=(
            Finding(
                expected="Cart total is $20",
                observed="Cart total is $25",
                result=FindingResult.FAILED,
                evidence=("cart.txt",),
            ),
        ),
        goal_verified=False,
        status=status,
        cleanup_status=CleanupStatus.FAILED,
    )


class ExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_dir = Path(self.temporary_directory.name) / "agent-data"
        self.store = ExecutionStore(self.data_dir)

    def save_run(self, application="shop", environment="staging", execution=None):
        return self.store.save_execution(
            application=application,
            environment=environment,
            execution=execution if execution is not None else sample_execution(),
        )

    def test_execution_survives_reopening_the_data_directory(self):
        execution_id = self.save_run()

        reopened = ExecutionStore(self.data_dir)
        loaded = reopened.get_execution(execution_id)

        self.assertEqual(loaded.findings[0].expected, "Cart total is $20")
        self.assertEqual(loaded.findings[0].observed, "Cart total is $25")
        self.assertEqual(loaded.findings[0].evidence, ("cart.txt",))
        self.assertEqual(loaded.overall_result, FindingResult.FAILED)
        self.assertEqual(loaded.cleanup_status, CleanupStatus.FAILED)

    def test_execution_listing_is_scoped_by_application_and_environment(self):
        staging_id = self.save_run("shop", "staging")
        self.save_run("shop", "production")
        self.save_run("portal", "staging")

        self.assertEqual(
            set(self.store.list_executions(application="shop", environment="staging")),
            {staging_id},
        )

    def test_different_data_directories_do_not_share_executions(self):
        execution_id = self.save_run()
        other = ExecutionStore(self.data_dir.parent / "other-data")

        self.assertEqual(other.list_executions(application="shop", environment="staging"), [])
        with self.assertRaises(KeyError):
            other.get_execution(execution_id)

    def test_interrupted_execution_retains_partial_findings_after_reopening(self):
        execution_id = self.save_run(execution=sample_execution(status=ExecutionStatus.INTERRUPTED))

        loaded = ExecutionStore(self.data_dir).get_execution(execution_id)

        self.assertEqual(loaded.status, ExecutionStatus.INTERRUPTED)
        self.assertEqual(loaded.findings[0].current_result, FindingResult.FAILED)
        self.assertEqual(loaded.findings[0].evidence, ("cart.txt",))
        self.assertIsNone(loaded.overall_result)

    def test_evidence_file_survives_reopening(self):
        execution_id = self.save_run()
        self.store.save_artifact(execution_id, "cart.txt", b"Cart total is $25")

        contents = ExecutionStore(self.data_dir).read_artifact(execution_id, "cart.txt")

        self.assertEqual(contents, b"Cart total is $25")

    def test_report_and_chronological_history_survive_reopening(self):
        execution_id = self.save_run()
        self.store.save_report(execution_id, "The cart total was incorrect.")
        self.store.append_history(execution_id, "Opened the cart")
        self.store.append_history(execution_id, "Observed a $25 total")

        reopened = ExecutionStore(self.data_dir)

        self.assertEqual(reopened.read_report(execution_id), "The cart total was incorrect.")
        self.assertEqual(
            reopened.read_history(execution_id),
            ["Opened the cart", "Observed a $25 total"],
        )

    def test_binary_evidence_is_preserved_without_text_conversion(self):
        execution_id = self.save_run()
        screenshot = b"\x89PNG\r\n\x1a\n\x00\xff"
        self.store.save_artifact(execution_id, "screenshots/cart.png", screenshot)

        self.assertEqual(self.store.read_artifact(execution_id, "screenshots/cart.png"), screenshot)

    def test_artifact_paths_cannot_escape_the_execution(self):
        execution_id = self.save_run()

        for name in ("../outside.txt", "/tmp/outside.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.save_artifact(execution_id, name, b"outside")

    def test_finding_review_and_revision_survive_reopening(self):
        execution_id = self.save_run()
        self.store.review_finding(
            execution_id, 0, FindingResult.PASSED, reason="The discount was applied separately"
        )
        self.store.review_finding(
            execution_id, 0, FindingResult.INCONCLUSIVE, reason="The discount evidence is incomplete"
        )

        finding = ExecutionStore(self.data_dir).get_execution(execution_id).findings[0]

        self.assertEqual(finding.original_result, FindingResult.FAILED)
        self.assertEqual(finding.current_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(
            [(review.result, review.reason) for review in finding.reviews],
            [
                (FindingResult.PASSED, "The discount was applied separately"),
                (FindingResult.INCONCLUSIVE, "The discount evidence is incomplete"),
            ],
        )

    def test_reviewed_result_is_used_after_reopening(self):
        execution = sample_execution()
        execution.goal_verified = True
        execution_id = self.save_run(execution=execution)
        self.store.review_finding(execution_id, 0, FindingResult.PASSED, reason="Expected value was wrong")

        loaded = ExecutionStore(self.data_dir).get_execution(execution_id)

        self.assertEqual(loaded.overall_result, FindingResult.PASSED)

    def test_review_without_a_reason_is_rejected_without_changing_history(self):
        execution_id = self.save_run()

        with self.assertRaises(ValueError):
            self.store.review_finding(execution_id, 0, FindingResult.PASSED, reason="")

        loaded = ExecutionStore(self.data_dir).get_execution(execution_id)
        self.assertEqual(loaded.findings[0].reviews, ())
        self.assertEqual(loaded.findings[0].current_result, FindingResult.FAILED)

    def test_deleting_a_run_removes_its_record_and_evidence_only(self):
        deleted_id = self.save_run()
        retained_id = self.save_run()
        self.store.save_artifact(deleted_id, "cart.txt", b"deleted run")
        self.store.save_artifact(retained_id, "cart.txt", b"retained run")
        self.store.save_report(deleted_id, "A report to delete")
        self.store.append_history(deleted_id, "A history entry to delete")
        deleted_files = [path for path in self.data_dir.rglob("cart.txt") if path.read_bytes() == b"deleted run"]
        self.assertEqual(len(deleted_files), 1)

        self.store.delete_execution(deleted_id)

        with self.assertRaises(KeyError):
            self.store.get_execution(deleted_id)
        with self.assertRaises((KeyError, FileNotFoundError)):
            self.store.read_artifact(deleted_id, "cart.txt")
        with self.assertRaises((KeyError, FileNotFoundError)):
            self.store.read_report(deleted_id)
        with self.assertRaises((KeyError, FileNotFoundError)):
            self.store.read_history(deleted_id)
        self.assertFalse(deleted_files[0].exists())
        self.assertEqual(self.store.read_artifact(retained_id, "cart.txt"), b"retained run")
        self.assertEqual(
            set(self.store.list_executions(application="shop", environment="staging")),
            {retained_id},
        )

    def test_unknown_execution_cannot_receive_evidence_or_a_review(self):
        with self.assertRaises(KeyError):
            self.store.save_artifact("missing", "cart.txt", b"data")
        with self.assertRaises(KeyError):
            self.store.review_finding("missing", 0, FindingResult.PASSED, reason="Reviewed")


if __name__ == "__main__":
    unittest.main()

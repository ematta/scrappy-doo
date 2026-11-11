"""Proposed knowledge lifecycle contract for coverage review."""

import tempfile
import unittest
from pathlib import Path

from scrappy_doo.execution import Execution
from scrappy_doo.knowledge import KnowledgeStore
from scrappy_doo.storage import ExecutionStore


class KnowledgeLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.data_dir = Path(temporary_directory.name) / "agent-data"
        self.executions = ExecutionStore(self.data_dir)
        self.knowledge = KnowledgeStore(self.data_dir)
        self.execution_id = self.executions.save_execution(
            application="shop",
            environment="staging",
            execution=Execution(findings=(), goal_verified=False),
        )
        self.executions.save_artifact(self.execution_id, "cart.txt", b"Cart menu")

    def propose(self, text="The cart is available from the main menu", *, environment=None):
        return self.knowledge.propose(
            application="shop",
            environment=environment,
            text=text,
            source_execution_id=self.execution_id,
            evidence=("cart.txt",),
        )

    def test_proposal_is_reviewable_but_not_used_as_approved_knowledge(self):
        proposal_id = self.propose()

        self.assertIn(proposal_id, self.knowledge.list_proposals(application="shop"))
        self.assertEqual(self.knowledge.list_approved(application="shop", environment="staging"), [])

    def test_approving_proposal_makes_it_available_and_removes_it_from_review_queue(self):
        proposal_id = self.propose()
        self.knowledge.approve(proposal_id)

        self.assertIn(proposal_id, self.knowledge.list_approved(application="shop", environment="staging"))
        self.assertNotIn(proposal_id, self.knowledge.list_proposals(application="shop"))

    def test_rejected_proposal_is_not_available_as_approved_knowledge(self):
        proposal_id = self.propose()
        self.knowledge.reject(proposal_id)

        self.assertNotIn(proposal_id, self.knowledge.list_proposals(application="shop"))
        self.assertNotIn(proposal_id, self.knowledge.list_approved(application="shop", environment="staging"))

    def test_editing_a_proposal_preserves_its_source_and_uses_the_edited_text(self):
        proposal_id = self.propose("The cart is always in the top bar")
        self.knowledge.edit(proposal_id, text="The cart is in the main menu")
        self.knowledge.approve(proposal_id)

        item = self.knowledge.get(proposal_id)
        self.assertEqual(item.text, "The cart is in the main menu")
        self.assertEqual(item.source_execution_id, self.execution_id)
        self.assertEqual(item.evidence, ("cart.txt",))

    def test_approved_knowledge_can_be_edited(self):
        knowledge_id = self.propose()
        self.knowledge.approve(knowledge_id)
        self.knowledge.edit(knowledge_id, text="The cart is under Shopping")

        self.assertEqual(self.knowledge.get(knowledge_id).text, "The cart is under Shopping")
        self.assertIn(knowledge_id, self.knowledge.list_approved(application="shop", environment="staging"))

    def test_retired_knowledge_is_not_used_but_remains_reviewable(self):
        knowledge_id = self.propose()
        self.knowledge.approve(knowledge_id)
        self.knowledge.retire(knowledge_id)

        self.assertNotIn(knowledge_id, self.knowledge.list_approved(application="shop", environment="staging"))
        self.assertEqual(self.knowledge.get(knowledge_id).text, "The cart is available from the main menu")

    def test_only_matching_application_and_environment_knowledge_is_selected(self):
        shared_id = self.propose("Shared shop workflow")
        staging_id = self.propose("Staging-only payment method", environment="staging")
        production_execution_id = self.executions.save_execution(
            application="shop", environment="production",
            execution=Execution(findings=(), goal_verified=False),
        )
        production_id = self.knowledge.propose(
            application="shop", environment="production", text="Production workflow",
            source_execution_id=production_execution_id, evidence=(),
        )
        other_execution_id = self.executions.save_execution(
            application="portal", environment="staging",
            execution=Execution(findings=(), goal_verified=False),
        )
        other_id = self.knowledge.propose(
            application="portal", environment=None, text="Portal workflow",
            source_execution_id=other_execution_id, evidence=(),
        )
        for knowledge_id in (shared_id, staging_id, production_id, other_id):
            self.knowledge.approve(knowledge_id)

        self.assertEqual(
            set(self.knowledge.list_approved(application="shop", environment="staging")),
            {shared_id, staging_id},
        )
        self.assertEqual(
            set(self.knowledge.list_approved(application="shop", environment="production")),
            {shared_id, production_id},
        )

    def test_knowledge_and_provenance_survive_reopening_the_data_directory(self):
        knowledge_id = self.propose()
        self.knowledge.approve(knowledge_id)

        reopened = KnowledgeStore(self.data_dir)
        item = reopened.get(knowledge_id)

        self.assertIn(knowledge_id, reopened.list_approved(application="shop", environment="staging"))
        self.assertEqual(item.source_execution_id, self.execution_id)
        self.assertEqual(item.evidence, ("cart.txt",))
        self.assertFalse(item.source_deleted)

    def test_approved_knowledge_survives_source_execution_deletion_with_provenance_marked(self):
        knowledge_id = self.propose()
        self.knowledge.approve(knowledge_id)

        self.executions.delete_execution(self.execution_id)

        reopened = KnowledgeStore(self.data_dir)
        item = reopened.get(knowledge_id)
        self.assertIn(knowledge_id, reopened.list_approved(application="shop", environment="staging"))
        self.assertEqual(item.source_execution_id, self.execution_id)
        self.assertTrue(item.source_deleted)

    def test_deleting_source_execution_removes_its_unapproved_proposals(self):
        proposal_id = self.propose()

        self.executions.delete_execution(self.execution_id)

        reopened = KnowledgeStore(self.data_dir)
        self.assertNotIn(proposal_id, reopened.list_proposals(application="shop"))
        with self.assertRaises(KeyError):
            reopened.get(proposal_id)

    def test_conflicting_proposal_keeps_approved_knowledge_until_reviewed(self):
        previous_id = self.propose("The cart is in the top bar")
        self.knowledge.approve(previous_id)
        proposal_id = self.knowledge.propose(
            application="shop", environment=None, text="The cart is in the main menu",
            source_execution_id=self.execution_id, evidence=("cart.txt",),
            conflicts_with=(previous_id,),
        )

        self.assertEqual(self.knowledge.get(proposal_id).conflicts_with, (previous_id,))
        self.assertEqual(self.knowledge.get(previous_id).text, "The cart is in the top bar")
        self.assertEqual(self.knowledge.list_approved(application="shop", environment="staging"), [previous_id])

    def test_proposal_cannot_use_a_source_execution_from_another_application(self):
        with self.assertRaises(ValueError):
            self.knowledge.propose(
                application="portal", environment=None, text="Portal workflow",
                source_execution_id=self.execution_id, evidence=(),
            )

    def test_unknown_source_execution_cannot_be_used_for_a_proposal(self):
        with self.assertRaises(KeyError):
            self.knowledge.propose(
                application="shop", environment=None, text="Unknown source",
                source_execution_id="missing", evidence=(),
            )


if __name__ == "__main__":
    unittest.main()

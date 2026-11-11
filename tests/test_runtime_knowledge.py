"""Proposed runtime knowledge integration contract for coverage review."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scrappy_doo.orchestration as orchestration
from scrappy_doo.cli import LocalRuntime, main
from scrappy_doo.execution import Execution, Finding, FindingResult
from scrappy_doo.knowledge import KnowledgeStore
from scrappy_doo.openrouter import OpenRouterError, OpenRouterModel
from scrappy_doo.storage import ExecutionStore


class KnowledgeRuntimeDouble:
    """External runtime boundary; real stores and CLI are used by each test."""

    def __init__(self):
        self.context = None
        self.artifacts = {"observations/cart.txt": b"Cart appears in the main menu"}
        self.proposals = ()

    def validate(self, *, application, environment):
        pass

    def run(self, *, application, environment, goal, approve, manual_sign_in):
        self.context = environment["approved_knowledge"]
        return Execution(findings=(), goal_verified=False)


class RuntimeKnowledgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "data"
        self.executions = ExecutionStore(self.data_dir)
        self.knowledge = KnowledgeStore(self.data_dir)
        self.source_id = self.executions.save_execution(
            application="shop", environment="staging", execution=Execution(findings=(), goal_verified=False),
        )
        self.runtime = KnowledgeRuntimeDouble()
        self.invoke(
            "environment", "add", "--application", "shop", "--name", "staging",
            "--url", "https://shop.example", "--allow-origin", "https://shop.example",
            "--boundary", "best-effort",
        )

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        status = main(
            ["--data-dir", str(self.data_dir), *args], input_fn=lambda prompt: "yes",
            stdout=output, stderr=error, runtime=self.runtime,
        )
        return status, output.getvalue(), error.getvalue()

    def propose(self, text, *, approve=False, environment=None, source_id=None, application="shop"):
        proposal_id = self.knowledge.propose(
            application=application, environment=environment, text=text,
            source_execution_id=source_id or self.source_id, evidence=(),
        )
        if approve:
            self.knowledge.approve(proposal_id)
        return proposal_id

    def run_goal(self):
        return self.invoke(
            "run", "--application", "shop", "--environment", "staging", "--goal", "Verify cart total",
        )

    def test_relevant_approved_knowledge_is_selected_for_the_goal(self):
        relevant_id = self.propose("The cart total is shown in the order summary", approve=True)
        unrelated_id = self.propose("Profile photos are changed in account settings", approve=True)

        selected = self.knowledge.select_relevant(application="shop", environment="staging", goal="Verify cart total")

        self.assertIn(relevant_id, selected)
        self.assertNotIn(unrelated_id, selected)

    def test_runtime_receives_only_relevant_approved_knowledge_for_selected_scope(self):
        shared = self.propose("Cart total appears in the order summary", approve=True)
        staging = self.propose("Staging cart total uses test currency", approve=True, environment="staging")
        self.propose("The cart total is under review")
        retired = self.propose("The cart total was in the header", approve=True)
        self.knowledge.retire(retired)
        production_source = self.executions.save_execution(
            application="shop", environment="production", execution=Execution(findings=(), goal_verified=False),
        )
        self.propose("Production cart total uses real currency", approve=True, environment="production", source_id=production_source)
        other_source = self.executions.save_execution(
            application="portal", environment="staging", execution=Execution(findings=(), goal_verified=False),
        )
        self.propose("Portal cart total is elsewhere", approve=True, source_id=other_source, application="portal")

        code, _, _ = self.run_goal()

        self.assertEqual(code, 0)
        self.assertEqual({item.id for item in self.runtime.context}, {shared, staging})

    def test_used_knowledge_ids_are_recorded_even_when_history_capture_is_disabled(self):
        knowledge_id = self.propose("Cart total is in the order summary", approve=True)
        self.invoke(
            "environment", "add", "--application", "shop", "--name", "private",
            "--url", "https://shop.example", "--allow-origin", "https://shop.example",
            "--boundary", "best-effort", "--no-history-capture",
        )

        code, _, _ = self.invoke(
            "run", "--application", "shop", "--environment", "private", "--goal", "Verify cart total",
        )

        self.assertEqual(code, 0)
        run_id = self.executions.list_executions(application="shop", environment="private")[0]
        self.assertEqual(self.executions.knowledge_used(run_id), (knowledge_id,))
        self.assertEqual(self.executions.read_history(run_id), [])

    def test_runtime_proposal_is_saved_for_review_with_run_and_evidence_provenance(self):
        self.runtime.proposals = (orchestration.KnowledgeProposal(
            text="The cart is in the main menu", environment="staging",
            evidence=("observations/cart.txt",), conflicts_with=(),
        ),)

        code, output, _ = self.run_goal()

        self.assertEqual(code, 0)
        proposal_id = self.knowledge.list_proposals(application="shop")[0]
        proposal = self.knowledge.get(proposal_id)
        run_id = self.executions.list_executions(application="shop", environment="staging")[-1]
        self.assertEqual(proposal.source_execution_id, run_id)
        self.assertEqual(proposal.environment, "staging")
        self.assertEqual(proposal.evidence, ("observations/cart.txt",))
        self.assertEqual(self.executions.read_artifact(run_id, "observations/cart.txt"), b"Cart appears in the main menu")
        self.assertNotIn(proposal_id, self.knowledge.list_approved(application="shop", environment="staging"))
        self.assertIn(proposal_id, output)

    def test_unsupported_proposal_evidence_is_not_saved_as_a_reviewable_fact(self):
        self.runtime.proposals = (orchestration.KnowledgeProposal(
            text="A guessed cart workflow", environment="staging",
            evidence=("not-captured.txt",), conflicts_with=(),
        ),)

        code, output, _ = self.run_goal()

        self.assertEqual(code, 0)
        self.assertEqual(self.knowledge.list_proposals(application="shop"), [])
        self.assertIn("proposal", output.lower())
        self.assertIn("evidence", output.lower())

    def test_conflicting_proposal_links_approved_knowledge_without_changing_it(self):
        old_id = self.propose("Cart total appears in the top bar", approve=True)
        self.runtime.proposals = (orchestration.KnowledgeProposal(
            text="Cart total appears in the main menu", environment="staging",
            evidence=("observations/cart.txt",), conflicts_with=(old_id,),
        ),)

        code, _, _ = self.run_goal()

        self.assertEqual(code, 0)
        proposal_id = self.knowledge.list_proposals(application="shop")[0]
        self.assertEqual(self.knowledge.get(proposal_id).conflicts_with, (old_id,))
        self.assertEqual(self.knowledge.get(old_id).text, "Cart total appears in the top bar")
        self.assertIn(old_id, self.knowledge.list_approved(application="shop", environment="staging"))

    def test_proposal_cannot_claim_a_different_environment(self):
        self.runtime.proposals = (orchestration.KnowledgeProposal(
            text="Cart total differs in production", environment="production",
            evidence=("observations/cart.txt",), conflicts_with=(),
        ),)

        code, output, _ = self.run_goal()

        self.assertEqual(code, 0)
        self.assertEqual(self.knowledge.list_proposals(application="shop"), [])
        self.assertIn("proposal", output.lower())

    def test_proposals_do_not_leak_into_another_data_directory(self):
        self.runtime.proposals = (orchestration.KnowledgeProposal(
            text="Cart total is visible", environment="staging",
            evidence=("observations/cart.txt",), conflicts_with=(),
        ),)
        self.run_goal()

        other = KnowledgeStore(self.data_dir.parent / "other")

        self.assertEqual(other.list_proposals(application="shop"), [])

    def test_real_runtime_passes_knowledge_to_model_and_persists_its_proposal(self):
        approved_id = self.propose("Cart total appears in the order summary", approve=True)
        recorded = {}

        class BrowserDouble:
            def __init__(self, *, config):
                pass

            def start(self):
                pass

            def close(self):
                pass

            def call(self, name, arguments):
                return "Cart page loaded"

            def begin_manual_sign_in(self):
                pass

            def finish_manual_sign_in(self):
                pass

            def as_tools(self):
                return {"browser_snapshot": orchestration.Tool(handler=lambda: "Cart is in the main menu")}

        class ModelDouble:
            def __init__(self, *, tool_schemas, model, approved_knowledge, required_initial_tool):
                recorded["context"] = approved_knowledge
                self.turn = 0

            def respond(self, *, goal, observations):
                self.turn += 1
                if self.turn == 1:
                    return orchestration.ToolCall(name="browser_snapshot", arguments={})
                return orchestration.FinalAnswer(
                    findings=(Finding(
                        expected="Verify cart total", observed="Cart is in the main menu",
                        result=FindingResult.INCONCLUSIVE, evidence=("observations/0001.txt",),
                        reason="The captured page does not show a cart total",
                    ),), goal_verified=False,
                    proposals=(orchestration.KnowledgeProposal(
                        text="Cart is in the main menu", environment="staging",
                        evidence=("observations/0001.txt",), conflicts_with=(),
                    ),),
                )

        self.runtime = LocalRuntime(browser_factory=BrowserDouble, model_factory=ModelDouble)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fictional-key"}), patch("shutil.which", return_value="npx"):
            code, _, error = self.run_goal()

        self.assertEqual(code, 0, error)
        self.assertEqual([item.id for item in recorded["context"]], [approved_id])
        proposal_id = self.knowledge.list_proposals(application="shop")[0]
        self.assertEqual(self.knowledge.get(proposal_id).text, "Cart is in the main menu")


class RecordingTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, *, url, headers, body):
        self.requests.append(body)
        return self.response


def provider_response(proposals):
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
            "goal_verified": False, "findings": [], "knowledge_proposals": proposals,
        })}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.0},
    }


class ProviderKnowledgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        executions = ExecutionStore(root)
        store = KnowledgeStore(root)
        source = executions.save_execution(application="shop", environment="staging", execution=Execution(findings=(), goal_verified=False))
        item_id = store.propose(application="shop", environment=None, text="Cart total is in the menu", source_execution_id=source, evidence=())
        store.approve(item_id)
        self.item = store.get(item_id)

    def test_provider_receives_approved_knowledge_as_data_with_its_id(self):
        transport = RecordingTransport(provider_response([]))
        model = OpenRouterModel(api_key="test-key", transport=transport, tool_schemas={}, approved_knowledge=(self.item,))

        model.respond(goal="Verify cart total", observations=())

        messages = transport.requests[0]["messages"]
        context = [message for message in messages if self.item.id in str(message.get("content"))]
        self.assertTrue(context)
        self.assertNotEqual(context[0]["role"], "system")
        self.assertIn("Cart total is in the menu", str(context[0]["content"]))

    def test_model_proposal_is_decoded_into_the_final_answer(self):
        response = provider_response([{
            "text": "Cart total is in the sidebar", "environment": "staging",
            "evidence": ["observations/cart.txt"], "conflicts_with": [self.item.id],
        }])
        model = OpenRouterModel(api_key="test-key", transport=RecordingTransport(response), tool_schemas={})

        answer = model.respond(goal="Verify cart total", observations=())

        self.assertEqual(answer.proposals[0].text, "Cart total is in the sidebar")
        self.assertEqual(answer.proposals[0].evidence, ("observations/cart.txt",))
        self.assertEqual(answer.proposals[0].conflicts_with, (self.item.id,))

    def test_malformed_proposal_is_rejected_instead_of_promoted(self):
        response = provider_response([{"text": "Cart changed", "evidence": "not-a-list"}])
        model = OpenRouterModel(api_key="test-key", transport=RecordingTransport(response), tool_schemas={})

        with self.assertRaises(OpenRouterError):
            model.respond(goal="Verify cart total", observations=())


if __name__ == "__main__":
    unittest.main()

#stopping early on the spend cap or deadline, and revisions that fix findings rather than only prose
import io
import json
import unittest
import contextlib
from unittest import mock

import tools
from panel import judge_agent
from pipeline import Pipeline
from state import BudgetExceeded, RunState
from tests.fakes import FakeClaude, first_message, schema_properties
from tests.test_pipeline import claude, PAGES, CANDIDATES, FakeWeb


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb(PAGES)
        for patcher in (mock.patch("requests.request", self.web), mock.patch("tools.search_free", lambda q, a: []),
                        mock.patch("pipeline.name_database_search", lambda name: CANDIDATES)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, reply, **patches):
        fake = FakeClaude(reply)
        with contextlib.redirect_stdout(io.StringIO()):
            run = Pipeline("Kant", client = fake.client(), save = False, log = lambda m: None)
            for name, value in patches.items():
                setattr(run, name, value)
            return fake, run.run()

    def test_spend_cap_in_the_debate_ships_what_is_finished(self):
        def broke(*args, **kwargs):
            raise BudgetExceeded("The $1.00 spend cap was used up before worker:con could run.")
        fake, report = self.run_with(claude, debate = broke)
        self.assertEqual(report["evaluation"]["failed"], ["not evaluated"])
        self.assertEqual(report["key_theory"]["name"], "The categorical imperative")  #the research agent's pick
        self.assertTrue(any(w.startswith("Stopped early: The $1.00 spend cap") and "The case for" in w and "Verdict" in w
                            for w in report["warnings"]))
        self.assertEqual(fake.calls_with("R5"), [])  #nothing more was spent

    def test_the_deadline_stops_new_stages(self):
        with mock.patch("state.RUN_TIMEOUT_S", 0):
            fake, report = self.run_with(claude)
        self.assertIn("limit passed before the debate", report["evaluation"]["stop_rule"])
        self.assertEqual(fake.calls_with("overrides_research_pick"), [])

    def test_unsupported_findings_are_discounted(self):
        graded = []

        #the first grading finds F1's page doesn't support it; the biography is then rewritten without F1
        def reply(body):
            props = schema_properties(body)
            if "R5" in props:
                content, stop = claude(body)
                grades = json.loads(content[0]["text"])
                if not graded:
                    grades["R5"] = {"score": 2, "feedback": "F1's page doesn't support the birth year.", "send_to": ["research"]}
                    grades["spot_checks"] = [dict(check, supports = "no" if check["id"] == "F1" else "yes") for check in grades["spot_checks"]]
                graded.append(body)
                return [{"type": "text", "text": json.dumps(grades)}], stop
            if set(props) == {"text", "finding_ids"}:
                return [{"type": "text", "text": '{"text": "Kant was a philosopher.", "finding_ids": ["F2"]}'}], "end_turn"
            return claude(body)

        fake, report = self.run_with(reply)
        self.assertEqual(len(graded), 2)
        self.assertNotIn("F1", [s["id"] for s in report["sources"]])
        self.assertIn("F1: discounted, because the evaluator found its page does not support it.", report["warnings"])
        self.assertEqual(report["life"]["text"], "Kant was a philosopher.")

    def test_panel_feedback_reaches_the_judges(self):
        fake = FakeClaude(lambda body: ([{"type": "text", "text": '{"score": 7, "justification": "j", "source": "F1"}'}], "end_turn"))
        judge_agent("rigour", {"question": "q"}, "material", ["F1"], "claude-sonnet-5-5", fake.client(), RunState("K", save = False), "R2: cite a source")
        self.assertIn("R2: cite a source", first_message(fake.requests[0]))


class SearchTest(unittest.TestCase):
    def test_wikisource_is_searched_for_routes_that_allow_it(self):
        self.assertIn("en.wikisource.org", tools.FREE_SEARCHES)
        asked = []
        with mock.patch.dict(tools.FREE_SEARCHES, {domain: (lambda d: lambda q: asked.append(d) or [])(domain) for domain in tools.FREE_SEARCHES}):
            tools.search_free("Analects", ["plato.stanford.edu", "en.wikisource.org"])
        self.assertEqual(sorted(asked), ["en.wikisource.org", "plato.stanford.edu"])


if __name__ == "__main__":
    unittest.main()

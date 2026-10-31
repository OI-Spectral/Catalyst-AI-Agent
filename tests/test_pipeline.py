#the whole pipeline end to end, against a fake Claude server and a fake web
import io
import json
import tempfile
import unittest
import contextlib
from unittest import mock

import pipeline
from pipeline import Pipeline, GateStopped
from tests.fakes import FakeClaude, FakeWeb, html_page, tool_use, text, schema_properties, first_message

SEP = "https://plato.stanford.edu/entries/"
PAGES = {
    SEP + "kant/": html_page("Immanuel Kant was born in Königsberg in 1724. The categorical imperative is the supreme principle of morality."),
    SEP + "kant-moral/": html_page("Korsgaard defends the formula of humanity as the ground of equal dignity. "
                                   "Hegel objected that the universalizability test is an empty formalism."),
}
SCORES = {"originality": 10, "influence": 10, "rigour": 8, "resilience": 6, "relevance": 8}


#plays every stage's model: the gate, router, research agent, orchestrator, workers, judges, verdict and grader
def claude(body):
    props = schema_properties(body)
    system = body.get("system") or ""
    asked = first_message(body)
    turns = len(body["messages"])
    may_use_tools = turns == 1 and body.get("tool_choice", {}).get("type") != "none"  #like the API, honour tool_choice none
    if "canonical_name" in props:
        return [text({"decision": "valid", "reason": "A philosopher.", "canonical_name": "Immanuel Kant", "options": []})], "end_turn"
    if "era" in props:
        return [text({"tradition": "early_modern", "era": "18th century", "domain": "ethics", "confidence": 0.93})], "end_turn"
    if "findings" in props:
        if may_use_tools:
            return [tool_use("r1", "fetch_page", {"url": SEP + "kant/", "find": ""})], "tool_use"
        return [text({
            "findings": [
                {"id": "F1", "claim": "Born 1724", "source_url": SEP + "kant/", "excerpt": "born in Königsberg in 1724", "status": "active"},
                {"id": "F2", "claim": "CI", "source_url": SEP + "kant/", "excerpt": "the supreme principle of morality", "status": "active"},
                {"id": "F3", "claim": "Invented", "source_url": SEP + "nowhere/", "excerpt": "x", "status": "active"},
            ],
            "biography": {"text": "Kant was born in Königsberg in 1724.", "finding_ids": ["F1", "F3"]},
            "key_theory": {"name": "The categorical imperative", "statement": "s", "why_chosen": "w", "finding_ids": ["F2"]},
        })], "end_turn"
    if "overrides_research_pick" in props:
        brief = {"objective": "o", "boundaries": "b", "finding_ids": ["F2"]}
        return [text({"theory": {"name": "The categorical imperative", "statement": "Act only on maxims you could will as universal laws.",
                                 "why_chosen": "Most discussed.", "finding_ids": ["F2"]},
                      "overrides_research_pick": False, "pro": brief, "con": brief})], "end_turn"
    if "premises" in props:
        side = "con" if "con worker" in system else "pro"
        if may_use_tools:
            return [tool_use(f"{side}1", "fetch_page", {"url": SEP + "kant-moral/", "find": ""})], "tool_use"
        excerpts = (["Korsgaard defends the formula of humanity", "the ground of equal dignity"] if side == "pro"
                    else ["Hegel objected that the universalizability test", "an empty formalism"])
        claim = "Strong objection." if "sent back" in asked else "Weak objection." if side == "con" else "It grounds dignity."
        return [text({"claim": claim, "premises": ["p1", "p2"], "conclusion": "c", "proponents": ["Korsgaard" if side == "pro" else "Hegel"],
                      "citations": [{"source_url": SEP + "kant-moral/", "excerpt": e} for e in excerpts]})], "end_turn"
    if "justification" in props:
        criterion = system.split("Criterion: ")[1].split(".")[0]
        return [text({"score": SCORES[criterion], "justification": f"{criterion} reasons", "source": "F2"})], "end_turn"
    if "verdict" in props:
        return [text({"verdict": "A major philosopher: 8.6 / 10."})], "end_turn"
    if "R5" in props:
        con_weak = "Weak objection." in asked
        grades = {cid: {"score": 5, "feedback": "good", "send_to": []} for cid in ("R5", "R6", "R7", "R8")}
        if con_weak:
            grades["R6"] = {"score": 2, "feedback": "The objection is a strawman.", "send_to": ["con"]}
        ids = props["spot_checks"]["items"]["properties"]["id"].get("enum", [])
        return [text({**grades, "spot_checks": [{"id": i, "supports": "yes"} for i in ids]})], "end_turn"
    raise AssertionError(f"unexpected request: {list(props)}")


CANDIDATES = [{"id": "Q9312", "label": "Immanuel Kant", "description": "German philosopher", "found_by_search": "Kant"}]


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClaude(claude)
        self.web = FakeWeb(PAGES)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for patcher in (mock.patch("requests.request", self.web), mock.patch("tools.search_free", lambda q, a: []),
                        mock.patch("pipeline.name_database_search", lambda name: CANDIDATES),
                        mock.patch("state.RUNS_DIR", self.folder.name), mock.patch("pipeline.RUNS_DIR", self.folder.name)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_pipeline(self, name = "Kant", **options):
        with contextlib.redirect_stdout(io.StringIO()):
            run = Pipeline(name, client = self.fake.client(), log = lambda message: None, **options)
            return run, run.run()

    def test_full_run_with_one_targeted_revision(self):
        run, report = self.run_pipeline(trace = True)

        self.assertEqual(report["philosopher"], "Immanuel Kant")
        self.assertEqual(report["route"], "early_modern")
        self.assertEqual(report["panel"]["weighted_score"], 8.6)  #computed in code from the five judges
        self.assertEqual(report["case_against"]["claim"], "Strong objection.")
        self.assertEqual(report["life"]["sources"], ["F1"])  #F3 was never fetched, so it was dropped
        self.assertIn("Dropped F3: https://plato.stanford.edu/entries/nowhere/ was never fetched.", report["warnings"])
        evaluation = report["evaluation"]
        self.assertEqual((evaluation["passed"], evaluation["rounds"], evaluation["stop_rule"]), (True, 1, "all criteria pass"))
        self.assertTrue(all(check["found_on_page"] and check["supports"] == "yes" for check in evaluation["spot_checks"]))

        #the failure went only to the con worker: pro ran once, con twice, then the panel, verdict and grader again
        workers = [body for body in self.fake.calls_with("premises") if len(body["messages"]) == 1]
        self.assertEqual(sorted("con" if "con worker" in b["system"] else "pro" for b in workers), ["con", "con", "pro"])
        self.assertIn("R6 Steelmanned (2/5): The objection is a strawman.", first_message(workers[-1]))
        for prop, count in (("canonical_name", 1), ("era", 1), ("overrides_research_pick", 1),
                            ("justification", 10), ("verdict", 2), ("R5", 2)):
            self.assertEqual(len(self.fake.calls_with(prop)), count, prop)
        grader = self.fake.calls_with("R5")[0]
        self.assertEqual(grader["model"], "claude-opus-5-5")  #a different model from the generators
        judge = self.fake.calls_with("justification")[0]
        self.assertNotIn("LIFE", first_message(judge))  #judges score the theory, not the biography

        #run_state.json and the trace
        saved = json.load(open(f"{run.state.folder}/run_state.json"))
        self.assertEqual(saved["status"], "passed")
        self.assertEqual(saved["progress"][:3], ["gate", "router", "research"])
        self.assertTrue(any(d.get("action") == "revise" and d["send_to"] == ["con"] for d in saved["decisions"]))
        self.assertEqual(saved["spent_usd"], report["usage"]["cost_usd"])
        self.assertEqual(len([c for c in saved["calls"] if c["type"] == "model"]), len(self.fake.requests))
        trace = [json.loads(line) for line in open(f"{run.state.folder}/trace.jsonl")]
        self.assertTrue(any(line["type"] == "tool" and line["stage"] == "evaluator" for line in trace))

    def test_estimate_prices_only_the_stages_that_rerun(self):
        run, report = self.run_pipeline()
        costs = run.step_costs
        self.assertAlmostEqual(run.estimate({"aggregator": "x"}), 1.25 * (costs["evaluate"] + costs["verdict"]))
        self.assertAlmostEqual(run.estimate({"con": "x"}), 1.25 * (costs["evaluate"] + 1.5 * costs["worker"] + costs["panel"] + costs["verdict"]))

    def test_spend_cap_stops_revisions(self):
        with mock.patch("state.MAX_RUN_COST_USD", 0.07):
            run, report = self.run_pipeline()
        self.assertFalse(report["evaluation"]["passed"])
        self.assertTrue(report["evaluation"]["stop_rule"].startswith("spend cap reached"))
        self.assertTrue(any(w.startswith("Failed R6 Steelmanned (2/5)") for w in report["warnings"]))
        self.assertLessEqual(report["usage"]["cost_usd"], 0.07)

    def test_dossier_cache(self):
        settings = pipeline.load_settings()
        settings["cache_dossiers"] = True
        self.run_pipeline(settings = settings)
        researched = len(self.fake.calls_with("findings"))
        self.run_pipeline(settings = settings)
        self.assertEqual(len(self.fake.calls_with("findings")), researched)  #the second run reused the dossier

    def test_gate_stops(self):
        for raw, outcome in (("Kant; DROP TABLE", "invalid"), ("", "invalid")):
            with self.assertRaises(GateStopped) as caught:
                self.run_pipeline(raw)
            self.assertEqual(caught.exception.outcome, outcome)
        self.assertEqual(self.fake.requests, [])  #sanitise stopped them before any model saw them

        ambiguous = FakeClaude(lambda body: ([text({"decision": "ambiguous", "reason": "Two philosophers share the name.", "canonical_name": "",
                                                    "options": ["Francis Bacon", "Roger Bacon"]})], "end_turn"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(GateStopped) as caught:
            Pipeline("Bacon", client = ambiguous.client(), log = lambda m: None).run()
        self.assertEqual(caught.exception.outcome, "ambiguous")
        self.assertEqual(caught.exception.options, ["Francis Bacon", "Roger Bacon"])
        self.assertIn("Did you mean: Francis Bacon, Roger Bacon?", caught.exception.message)


class CliTest(unittest.TestCase):
    def test_json_output_is_only_json(self):
        import CLI
        report = {"philosopher": "Immanuel Kant"}

        class FakePipeline:
            def __init__(self, name, trace = False):
                print("progress that must not reach stdout")

            def run(self):
                return report

        out = io.StringIO()
        with mock.patch("CLI.Pipeline", FakePipeline), mock.patch("sys.argv", ["CLI.py", "Immanuel", "Kant", "--json"]), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            CLI.main()
        self.assertEqual(json.loads(out.getvalue()), report)


if __name__ == "__main__":
    unittest.main()

import copy
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml

import costs
import llm
import agent
import research
import router
import workers
import orchestrator
import panel
import report as report_module
import evaluator
import settings as settings_module
from state import RunState, BudgetExceeded
from tests.fakes import FakeClaude, FakeWeb, html_page, tool_use, text, usage

ROUTES = router.load_routes()
EM_ROUTE = ROUTES["routes"]["early_modern"]
EM = EM_ROUTE["allowlist"]
SETTINGS = settings_module.load_settings()
RUBRIC = evaluator.load_rubric()
KANT = "https://plato.stanford.edu/entries/kant/"


def block(kind, **fields):
    return SimpleNamespace(type = kind, **fields)


class CostsTest(unittest.TestCase):
    def test_plain_call(self):
        self.assertAlmostEqual(costs.call_cost("claude-sonnet-5-5", usage()), (1000 * 2 + 100 * 10) / 1e6)

    def test_fallback_prices_each_model_that_ran(self):
        iterations = [SimpleNamespace(type = "message", model = None, **vars(usage())),
                      SimpleNamespace(type = "fallback_message", model = "claude-opus-5-5", **vars(usage()))]
        response = SimpleNamespace(model = "claude-opus-5-5", usage = usage(iterations = iterations))
        expected = (1000 * 2 + 100 * 10 + 1000 * 4 + 100 * 20) / 1e6
        self.assertAlmostEqual(costs.response_cost(response, "claude-sonnet-5-5"), expected)


class FallbackBlocksTest(unittest.TestCase):
    def test_echo_drops_a_declined_models_thinking_and_tool_calls(self):
        blocks = [block("thinking"), block("text", text = "partial"), block("tool_use"), block("fallback"), block("tool_use")]
        echo = llm.content_to_echo(SimpleNamespace(content = blocks))
        self.assertEqual([b.type for b in echo], ["text", "tool_use"])

    def test_answer_comes_from_the_model_that_answered(self):
        blocks = [block("text", text = "declined"), block("fallback"), block("text", text = '{"a": 1}')]
        self.assertEqual(llm.answer_text(SimpleNamespace(content = blocks)), '{"a": 1}')


class SettingsTest(unittest.TestCase):
    def load_with(self, change):
        settings = yaml.safe_load(open(settings_module.SETTINGS_FILE))
        change(settings)
        with tempfile.NamedTemporaryFile("w", suffix = ".yaml", delete = False) as f:
            yaml.safe_dump(settings, f)
        return settings_module.load_settings(f.name)

    def test_rejects_bad_settings(self):
        for change in (lambda s: s["criteria"]["originality"].update(weight = 0.5),
                       lambda s: s["models"].update(evaluator = s["models"]["judges"]),
                       lambda s: s.update(judge_votes = 2),
                       lambda s: s["budget_shares"].update(research = 0.8)):
            with self.assertRaises(ValueError):
                self.load_with(change)

    def test_rubric_must_match_the_code_checks(self):
        rubric = yaml.safe_load(open(evaluator.RUBRIC_FILE))
        del rubric["criteria"]["R4"]
        with tempfile.NamedTemporaryFile("w", suffix = ".yaml", delete = False) as f:
            yaml.safe_dump(rubric, f)
        with self.assertRaises(ValueError):
            evaluator.load_rubric(f.name)


class StateTest(unittest.TestCase):
    def test_spend_cap_refuses_the_call(self):
        fake = FakeClaude(lambda body: ([text({})], "end_turn"))
        state = RunState("Kant", save = False, cap_usd = 0.001)
        client = state.client_for(fake.client(), "test")
        client.beta.messages.create(model = "claude-sonnet-5-5", max_tokens = 10, messages = [{"role": "user", "content": "hi"}])
        self.assertAlmostEqual(state.spent, 0.003)
        with self.assertRaises(BudgetExceeded):
            client.beta.messages.create(model = "claude-sonnet-5-5", max_tokens = 10, messages = [{"role": "user", "content": "hi"}])
        self.assertEqual(len(fake.requests), 1)  #the refused call never reached the API

    def test_saves_state_and_trace(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("state.RUNS_DIR", folder):
            state = RunState("Zera Yacob", trace = True)
            self.assertTrue(state.run_id.endswith("-zera-yacob"))
            state.record_tool_call("research", "search", {"query": "q"}, "results", 0.1)
            state.decide("router", route = "non_western")
            state.finish("router")
            saved = yaml.safe_load(open(f"{state.folder}/run_state.json"))
            self.assertEqual(saved["progress"], ["router"])
            self.assertEqual(saved["decisions"][0]["route"], "non_western")
            self.assertIn('"output": "results"', open(f"{state.folder}/trace.jsonl").read())


class RouterTest(unittest.TestCase):
    def test_pick_route(self):
        for decision, expect in (({"tradition": "early_modern", "confidence": 0.94}, "early_modern"),
                                 ({"tradition": "early_modern", "confidence": 0.7}, "early_modern"),
                                 ({"tradition": "early_modern", "confidence": 0.69}, "general"),
                                 ({"tradition": "made_up", "confidence": 0.99}, "general"),
                                 ({"tradition": "ancient", "confidence": 94}, "general"),
                                 ({"tradition": "ancient", "confidence": True}, "general"),
                                 ({"tradition": "ancient"}, "general")):
            self.assertEqual(router.pick_route(decision, ROUTES)[0], expect, decision)

    def test_missing_fallback_is_rejected(self):
        routes = yaml.safe_load(open(router.ROUTES_FILE))
        routes["fallback"] = "nowhere"
        with tempfile.NamedTemporaryFile("w", suffix = ".yaml", delete = False) as f:
            yaml.safe_dump(routes, f)
        with self.assertRaises(ValueError):
            router.load_routes(f.name)


class DossierTest(unittest.TestCase):
    def test_check_dossier(self):
        pages = {agent.url_key(KANT): "<retrieved_document>Kant was born in Königsberg in 1724 — a Prussian city.</retrieved_document>"}
        dossier = {
            "findings": [
                {"id": "F1", "claim": "born 1724", "source_url": "http://plato.stanford.edu/entries/kant", "excerpt": "Kant was born in Königsberg in 1724 - a Prussian", "status": "active"},
                {"id": "F2", "claim": "x", "source_url": "https://plato.stanford.edu/entries/never/", "excerpt": "x", "status": "active"},
                {"id": "F3", "claim": "x", "source_url": "https://evil.com/", "excerpt": "x", "status": "active"},
                {"id": "F1", "claim": "dup", "source_url": KANT, "excerpt": "Kant", "status": "active"},
                {"id": "F4", "claim": "made up", "source_url": KANT + "#life", "excerpt": "Kant loved football", "status": "discounted"},
            ],
            "biography": {"text": "word " * 201, "finding_ids": ["F1", "F2", "F1"]},
            "key_theory": {"name": "CI", "statement": "s", "why_chosen": "w", "finding_ids": ["F4"]},
        }
        warnings = research.check_dossier(dossier, EM, pages)
        self.assertEqual([f["id"] for f in dossier["findings"]], ["F1", "F4"])
        self.assertFalse(any(w.startswith("F1:") for w in warnings))  #matches despite dash, http and slash differences
        self.assertTrue(any(w.startswith("F4:") for w in warnings))
        self.assertEqual(dossier["biography"]["finding_ids"], ["F1"])
        self.assertEqual(dossier["key_theory"]["finding_ids"], [])
        self.assertIn("key_theory: no active findings behind it.", warnings)
        self.assertIn("biography: 201 words; the limit is 200.", warnings)


class AgentLoopTest(unittest.TestCase):
    def test_limits_tools_and_forces_an_answer(self):
        answer = {"findings": [], "biography": {"text": "b", "finding_ids": []}, "key_theory": {"name": "n", "statement": "s", "why_chosen": "w", "finding_ids": []}}

        def reply(body):
            turn = len(body["messages"])
            if turn == 1:
                return [{"type": "thinking", "thinking": "", "signature": "sig"}, tool_use("t1", "search", {"query": "kant"}),
                        tool_use("t2", "fetch_page", {"url": "https://evil.com/", "find": ""})], "tool_use"
            if turn == 3:
                return [tool_use("t3", "fetch_page", {"url": KANT, "find": ""}), tool_use("t4", "fetch_page", {"url": KANT, "find": "ethics"})], "tool_use"
            return [text(answer)], "end_turn"

        fake = FakeClaude(reply)
        state = RunState("Kant", save = False)
        web = FakeWeb({KANT: html_page("Kant’s ethics")})
        with mock.patch("requests.request", web), mock.patch("tools.search_free", lambda q, a: []):
            result, pages, used = agent.agent_loop(state.client_for(fake.client(), "research"), state, "claude-sonnet-5-5", "system",
                                                   "Research Kant", research.DOSSIER_SCHEMA, EM, 3, 1.0)
        first, second, last = fake.requests
        self.assertEqual([r["tool_choice"] for r in fake.requests], [{"type": "auto"}, {"type": "auto"}, {"type": "none"}])
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertEqual(first["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(second["messages"][1]["content"][0]["type"], "thinking")  #thinking echoed back
        self.assertTrue(any(r.get("is_error") and "Refused" in r["content"] for r in second["messages"][-1]["content"]))
        final = last["messages"][-1]["content"]
        self.assertTrue(any(r.get("tool_use_id") == "t4" and r.get("is_error") for r in final))  #4th call is past the limit
        self.assertIn("no tool calls left", final[-1]["text"])
        self.assertEqual(used["tool_calls"], 4)
        self.assertIn(agent.url_key(KANT), pages)
        self.assertEqual(len([c for c in state.calls if c["type"] == "tool"]), 3)

    def test_stops_if_tools_are_called_after_the_limit(self):
        fake = FakeClaude(lambda body: ([tool_use(f"t{len(body['messages'])}", "search", {"query": "q"})], "tool_use"))
        state = RunState("Kant", save = False)
        with mock.patch("tools.search_free", lambda q, a: []):
            with self.assertRaises(RuntimeError):
                agent.agent_loop(state.client_for(fake.client(), "research"), state, "claude-sonnet-5-5", "s", "p",
                                 research.DOSSIER_SCHEMA, EM, 1, 1.0)


def argument(citations, claim = "c"):
    return {"claim": claim, "premises": ["p1", "p2"], "conclusion": "so", "proponents": ["Korsgaard"], "citations": citations}


class WorkerTest(unittest.TestCase):
    def test_check_argument(self):
        pages = {agent.url_key(KANT): "the categorical imperative binds all rational agents"}
        good = {"source_url": KANT, "excerpt": "binds all rational agents"}
        arg = argument([good, {"source_url": "https://evil.com/", "excerpt": "x"}, {"source_url": KANT + "x/", "excerpt": "x"}])
        problems, warnings = workers.check_argument(arg, EM, pages)
        self.assertEqual(arg["citations"], [good])
        self.assertEqual(len(warnings), 2)
        self.assertIn("1 usable citations", problems[0])
        long_arg = argument([good, good], claim = "word " * 251)
        self.assertIn("254 words", workers.check_argument(long_arg, EM, pages)[0][0])  #251 in the claim, 3 in the premises and conclusion


def plan():
    brief = {"objective": "o", "boundaries": "b", "finding_ids": ["F1"]}
    return {"theory": {"name": "CI", "statement": "Act on universal maxims.", "why_chosen": "w", "finding_ids": ["F1"]},
            "overrides_research_pick": False, "pro": dict(brief), "con": dict(brief, finding_ids = [])}


DOSSIER = {"philosopher": "Immanuel Kant", "warnings": [],
           "findings": [{"id": "F1", "claim": "CI is supreme", "source_url": KANT, "excerpt": "supreme principle", "status": "active"},
                        {"id": "F2", "claim": "gone", "source_url": KANT, "excerpt": "x", "status": "discounted"}],
           "biography": {"text": "Born 1724.", "finding_ids": ["F1"]},
           "key_theory": {"name": "CI", "statement": "s", "why_chosen": "w", "finding_ids": ["F1"]}}


class OrchestratorTest(unittest.TestCase):
    def test_brief_follows_the_contract(self):
        brief = orchestrator.render_brief("pro", plan(), DOSSIER, EM_ROUTE, 12)
        for line in ("OBJECTIVE   o", "BOUNDARIES  b", "CONTEXT     Theory: CI. Act on universal maxims.", "[F1] CI is supreme",
                     "TOOLS       search, fetch_page (allowed sites: " + ", ".join(EM), "STOP WHEN   One argument with at least 2 citations, or 12 tool calls used."):
            self.assertIn(line, brief)
        self.assertNotIn("[F1]", orchestrator.render_brief("con", plan(), DOSSIER, EM_ROUTE, 12))

    def test_worker_is_sent_back_once_with_the_reason(self):
        calls = []

        def fake_worker(side, brief, route, model, client, state, max_tool_calls, budget, pages, feedback = None):
            calls.append((side, feedback))
            fixed = side == "pro" or "usable citations" in (feedback or "")
            return {"side": side, **argument([]), "problems": [] if fixed else ["It has 0 usable citations."], "warnings": []}

        state = RunState("Kant", save = False)
        with mock.patch("orchestrator.worker_agent", fake_worker):
            result = orchestrator.dispatch_workers(["pro", "con"], plan(), DOSSIER, EM_ROUTE, {}, SETTINGS, None, state, {"con": "R6: weak"})
        self.assertEqual(sorted(result), ["con", "pro"])
        self.assertEqual(sorted(calls), [("con", "R6: weak"), ("con", "R6: weak It has 0 usable citations."), ("pro", None)])
        self.assertEqual(state.decisions[0]["action"], "re-dispatched")


class PanelTest(unittest.TestCase):
    def test_weighted_score_is_code_maths(self):
        scores = [{"weight": w, "score": s} for w, s in zip((0.25, 0.25, 0.2, 0.2, 0.1), (10, 10, 8, 6, 8))]
        self.assertEqual(panel.weighted_score(scores), 8.6)  #the README's example
        scores = [{"weight": w, "score": s} for w, s in zip((0.25, 0.25, 0.2, 0.2, 0.1), (9, 8, 8, 8, 8))]
        self.assertEqual(panel.weighted_score(scores), 8.3)  #8.25 rounds half up, not to even

    def test_median_vote(self):
        votes = [{"score": 3, "justification": "a"}, {"score": 9, "justification": "b"}, {"score": 5, "justification": "c"}]
        self.assertEqual(panel.median_vote(votes)["justification"], "c")


def full_report():
    arguments = {side: argument([{"source_url": KANT, "excerpt": f"{side} quote"}] * 2) for side in ("pro", "con")}
    scores = [{"criterion": c, "weight": spec["weight"], "score": 8, "justification": "j", "sources": ["F1"]} for c, spec in SETTINGS["criteria"].items()]
    return report_module.assemble("Immanuel Kant", "early_modern", DOSSIER, plan(), arguments,
                                  {"scores": scores, "weighted_score": 8.0}, {"text": "A strong verdict at 8.0 / 10."})


class ReportTest(unittest.TestCase):
    def test_assemble(self):
        report = full_report()
        self.assertEqual([s["id"] for s in report["sources"]], ["F1", "P1", "P2", "C1", "C2"])  #the discounted F2 is left out
        self.assertEqual(report["case_against"]["sources"], ["C1", "C2"])
        self.assertEqual(report_module.check_shape(report, report_module.report_schema(list(SETTINGS["criteria"]))), [])
        self.assertIn("# Immanuel Kant\n", report_module.render_markdown(report))
        self.assertNotIn("LIFE", report_module.report_text(report, leave_out = ("life",)))

    def test_assemble_is_a_snapshot(self):
        dossier = copy.deepcopy(DOSSIER)
        report = report_module.assemble("K", "r", dossier, plan(), {s: argument([]) for s in ("pro", "con")})
        dossier["biography"]["finding_ids"].append("F9")
        self.assertEqual(report["life"]["sources"], ["F1"])

    def test_compare(self):
        first, second = full_report(), full_report()
        second["philosopher"], second["panel"]["weighted_score"] = "David Hume", 7.5
        self.assertIn("Immanuel Kant scores higher (8.0 vs 7.5)", report_module.compare(first, second))


class CodeChecksTest(unittest.TestCase):
    def check(self, report):
        return evaluator.code_checks(report, EM, RUBRIC, list(SETTINGS["criteria"]))[0]

    def test_a_good_report_passes(self):
        self.assertTrue(all(result["passed"] for result in self.check(full_report()).values()))

    def test_each_failure_goes_to_its_owner(self):
        report = full_report()
        report["life"]["sources"] = []
        report["sources"][1]["url"] = "https://evil.com/"
        report["verdict"]["text"] = "word " * 201
        report["panel"]["scores"].pop()
        results = self.check(report)
        self.assertEqual(results["R1"]["send_to"], ["panel"])
        self.assertEqual(results["R2"]["send_to"], ["research"])
        self.assertEqual(results["R3"]["send_to"], ["pro"])
        self.assertEqual(results["R4"]["send_to"], ["aggregator"])

    def test_a_broken_shape_fails_r1_only_with_a_reason(self):
        report = full_report()
        del report["case_for"]
        results, shape_ok = evaluator.code_checks(report, EM, RUBRIC, list(SETTINGS["criteria"]))
        self.assertFalse(shape_ok)
        self.assertIn("report.case_for is missing.", results["R1"]["feedback"])
        self.assertEqual(results["R1"]["send_to"], ["pro"])


def graded(scores):
    criteria = {cid: {"name": cid, "check": "model", "passed": s >= 4, "score": s, "feedback": "fix", "send_to": [] if s >= 4 else ["con"]}
                for cid, s in scores.items()}
    return {"passed": all(s >= 4 for s in scores.values()), "failed": [c for c, s in scores.items() if s < 4], "criteria": criteria, "spot_checks": []}


class EvaluationLoopTest(unittest.TestCase):
    def run_loop(self, rounds_of_scores, estimate = 0.0, max_revisions = 3):
        results = iter(rounds_of_scores)
        revisions = []
        state = RunState("Kant", save = False, cap_usd = 1.0)
        report, evaluation = evaluator.evaluation_loop(
            {"round": 0}, lambda report: graded(next(results)),
            lambda feedback, result: revisions.append(feedback) or {"round": len(revisions)},
            lambda feedback: estimate, state, max_revisions)
        return report, evaluation, revisions

    def test_stops_when_all_pass(self):
        report, evaluation, revisions = self.run_loop([{"R6": 2}, {"R6": 5}])
        self.assertEqual((evaluation["stop_rule"], evaluation["rounds"], report), ("all criteria pass", 1, {"round": 1}))
        self.assertEqual(revisions, [{"con": "R6 R6 (2/5): fix"}])

    def test_stops_when_nothing_improves_and_ships_the_best(self):
        report, evaluation, _ = self.run_loop([{"R6": 3, "R7": 3}, {"R6": 2, "R7": 3}])
        self.assertEqual(evaluation["stop_rule"], "no score improved since the last round")
        self.assertEqual(report, {"round": 0})  #the first report graded better

    def test_stops_after_max_revisions(self):
        _, evaluation, revisions = self.run_loop([{"R6": 1}, {"R6": 2}, {"R6": 3}, {"R6": 3.5}], max_revisions = 3)
        self.assertEqual((evaluation["stop_rule"], len(revisions)), ("3 revision rounds used", 3))

    def test_stops_before_a_round_it_cannot_afford(self):
        _, evaluation, revisions = self.run_loop([{"R6": 2}], estimate = 5.0)
        self.assertTrue(evaluation["stop_rule"].startswith("spend cap reached"))
        self.assertEqual(revisions, [])

    def test_ships_ungraded_if_the_evaluator_cannot_run(self):
        def no_budget(report):
            raise BudgetExceeded("cap")
        report, evaluation = evaluator.evaluation_loop({"r": 1}, no_budget, None, None, RunState("K", save = False))
        self.assertEqual((report, evaluation["failed"]), ({"r": 1}, ["not evaluated"]))


if __name__ == "__main__":
    unittest.main()

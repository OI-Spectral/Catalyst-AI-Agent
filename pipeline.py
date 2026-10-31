import os
import json

from anthropic import Anthropic

from validity_gate import sanitise, name_database_search, philosopher_judge_agent, AmbiguousNameError
from router import load_routes, router_agent
from research import research_agent, revise_biography
from orchestrator import orchestrator_agent, dispatch_workers
from panel import run_panel, verdict_agent
from evaluator import load_rubric, evaluate, evaluation_loop
from report import assemble, SECTION_OWNERS, TITLES
from tools import MAX_RETRIES
from llm import with_one_retry
from settings import load_settings
from state import RunState, RunStopped, TimeUp, RUNS_DIR, slug


#the validity gate stopped the run; outcome is "ambiguous" (options lists the choices) or "invalid"
class GateStopped(Exception):
    def __init__(self, outcome, message, options = ()):
        super().__init__(message)
        self.outcome = outcome
        self.message = message
        self.options = list(options)


#runs the five stages in order and enforces the limits between them.
#Each stage's output is kept here, so a revision can re-run one stage and reuse the rest.
class Pipeline:
    def __init__(self, raw_name, client = None, trace = False, save = True, log = print,
                 settings = None, routes = None, rubric = None):
        self.raw_name = raw_name
        self.client = client or Anthropic(max_retries = MAX_RETRIES)  #the SDK retries rate limits and server errors with backoff
        self.settings = settings or load_settings()
        self.routes = routes or load_routes()
        self.rubric = rubric or load_rubric()
        self.state = RunState(raw_name, trace = trace, save = save)
        self.log = log
        self.step_costs = {}  #what each step cost the last time it ran, to price a revision round
        self.plan = self.panel = self.verdict = None
        self.arguments = {}

    #run one step and remember what it cost
    def step(self, name, fn, *args, **kwargs):
        before = self.state.spent
        result = fn(*args, **kwargs)
        self.step_costs[name] = self.state.spent - before
        return result

    #the run's full pipeline. Once there is a dossier, hitting the spend cap or the deadline
    #stops new work and ships the sections finished so far, with a warning.
    def run(self):
        try:
            self.gate()
            self.route()
            return self.build_report()
        except GateStopped:
            raise
        except BaseException as e:
            self.state.status = f"failed: {type(e).__name__}: {e}"
            self.state.save()
            raise

    #stages 2b to 5: research, then the rest, stopping early (with a report) if a limit is hit
    def build_report(self):
        self.research()
        try:
            self.check_time("the debate")
            self.debate()
            self.check_time("the panel")
            self.judge()
            self.check_time("the evaluator")
            return self.review()
        except RunStopped as e:
            return self.stop_early(str(e))

    def check_time(self, next_step):
        if self.state.time_left() <= 0:
            raise TimeUp(f"The run's {self.state.deadline - self.state.started:.0f} s limit passed before {next_step}.")

    #1. validity gate (validity_gate.py)
    def gate(self):
        self.log("1. Validity gate")
        try:
            name = sanitise(self.raw_name)
            self.name = philosopher_judge_agent(name, name_database_search(name), self.state.client_for(self.client, "gate"))
        except json.JSONDecodeError:
            raise  #a broken reply from Wikidata or the model, not a verdict on the name
        except AmbiguousNameError as e:
            self.stop_at_gate("ambiguous", str(e), e.options)
        except ValueError as e:
            self.stop_at_gate("invalid", str(e))
        try:
            self.name = sanitise(self.name)  #the model's name goes into every later prompt, so check it like typed input
        except ValueError:
            self.name = name
        self.state.decide("gate", outcome = "valid", name = self.name)
        self.state.finish("gate")
        return self.name

    def stop_at_gate(self, outcome, message, options = ()):
        self.state.decide("gate", outcome = outcome, reason = message, options = list(options))
        self.state.status = f"stopped at the gate ({outcome})"
        self.state.save()
        raise GateStopped(outcome, message, options)

    #2a. router: the model classifies, code picks the route
    def route(self):
        self.log("2. Router")
        self.route_name, self.route_config, self.router_decision = router_agent(self.name, self.client, self.state, self.routes,
                                                                                self.settings["models"]["router"])
        if self.route_config["model"] == self.settings["models"]["evaluator"]:
            raise ValueError(f"The {self.route_name} route's research model is the evaluator's model; they must differ.")
        self.state.finish("router")

    #2b. research agent for the chosen route (or a saved dossier, if cache_dossiers is on)
    def research(self):
        self.log(f"   Research agent ({self.route_name} route)")
        cache = os.path.join(RUNS_DIR, "cache", f"{slug(self.name)}-{self.route_name}.json")
        if self.settings["cache_dossiers"] and os.path.exists(cache):
            with open(cache) as f:
                saved = json.load(f)
            self.dossier, self.pages = saved["dossier"], saved["pages"]
            self.state.decide("research", action = "reused a saved dossier", path = cache)
        else:
            budget = self.settings["budget_shares"]["research"] * self.state.cap_usd
            self.dossier, self.pages = self.step("research", research_agent, self.name, self.route_name, self.route_config,
                                                 self.client, self.state, budget)
            if self.settings["cache_dossiers"]:
                os.makedirs(os.path.dirname(cache), exist_ok = True)
                with open(cache, "w") as f:
                    json.dump({"dossier": self.dossier, "pages": self.pages}, f, ensure_ascii = False)
        self.dossier["router"] = self.router_decision
        self.state.findings = self.dossier["findings"]
        self.state.finish("research")

    #3. orchestrator picks and states the theory, then the pro and con workers run in parallel
    def debate(self, feedback = None, sides = ("pro", "con")):
        feedback = feedback or {}
        if feedback.get("orchestrator") or not feedback:
            self.log("3. Orchestrator + workers")
            self.plan = self.step("orchestrator", with_one_retry, orchestrator_agent, self.dossier, self.settings["models"]["orchestrator"],
                                  self.client, self.state, feedback.get("orchestrator"))
            self.state.decide("orchestrator", theory = self.plan["theory"]["name"],
                              overrides_research_pick = self.plan["overrides_research_pick"])
        before = self.state.spent
        arguments = dispatch_workers(list(sides), self.plan, self.dossier, self.route_config, self.pages,
                                     self.settings, self.client, self.state, {side: feedback.get(side) for side in sides})
        self.step_costs["worker"] = (self.state.spent - before) / len(sides)
        self.arguments.update(arguments)
        self.state.finish("workers")

    #4. five judges in parallel, the weighted score in code, then the verdict
    def judge(self, verdict_feedback = None, panel_feedback = None, rescore = True):
        self.log("4. Panel + aggregator")
        if rescore:
            self.panel = self.step("panel", run_panel, self.assemble(), self.settings, self.client, self.state, panel_feedback)
        self.verdict = self.step("verdict", with_one_retry, verdict_agent, self.assemble(), self.settings["models"]["aggregator"],
                                 self.client, self.state, verdict_feedback)
        self.state.finish("panel")

    def assemble(self):
        return assemble(self.name, self.route_name, self.dossier, self.plan, self.arguments, self.panel, self.verdict)

    def grade(self, report):
        return self.step("evaluate", evaluate, report, self.route_config["allowlist"], self.rubric, self.settings,
                         self.client, self.state)

    #findings the evaluator's spot checks found unsupported by their own page are discounted, so nothing cites them
    def discount_unsupported(self, result):
        findings = {finding["id"]: finding for finding in self.dossier["findings"] if finding["status"] == "active"}
        unsupported = [check["id"] for check in result["spot_checks"] if check["supports"] == "no" and check["id"] in findings]
        for finding_id in unsupported:
            findings[finding_id]["status"] = "discounted"
            self.dossier["warnings"].append(f"{finding_id}: discounted, because the evaluator found its page does not support it.")
        if unsupported:
            self.state.decide("evaluator", action = "discounted findings", findings = unsupported)

    #re-run only the stages that own a failure, plus the stages that read their output
    def revise(self, feedback, result):
        self.log("   Revising: " + ", ".join(sorted(feedback)))
        self.state.decide("evaluator", action = "revise", send_to = sorted(feedback))
        if "research" in feedback:
            self.discount_unsupported(result)
            self.step("biography", revise_biography, self.dossier, feedback["research"], self.route_config["model"],
                      self.client, self.state)
        sides = ["pro", "con"] if "orchestrator" in feedback else [side for side in ("pro", "con") if side in feedback]
        if sides:
            self.debate(feedback, sides)
        if sides or "panel" in feedback or "aggregator" in feedback:
            self.judge(feedback.get("aggregator"), feedback.get("panel"), rescore = bool(sides) or "panel" in feedback)
        return self.assemble()

    #what a revision round would cost, from what each step cost last time. It errs high: a worker
    #can be sent back again (up to half its cost once more), and 25% is added for variation.
    def estimate(self, feedback):
        costs = self.step_costs
        sides = 2 if "orchestrator" in feedback else len([side for side in ("pro", "con") if side in feedback])
        cost = costs.get("evaluate", 0)
        if "research" in feedback:
            cost += costs.get("biography", 0.01)
        if "orchestrator" in feedback:
            cost += costs.get("orchestrator", 0)
        cost += sides * costs.get("worker", 0) * 1.5
        if sides or "panel" in feedback:
            cost += costs.get("panel", 0)
        if sides or "panel" in feedback or "aggregator" in feedback:
            cost += costs.get("verdict", 0)
        return cost * 1.25

    #5. evaluator loop, then the final report
    def review(self):
        self.log("5. Evaluator")
        report, evaluation = evaluation_loop(self.assemble(), self.grade, self.revise, self.estimate, self.state)
        return self.finish(report, evaluation)

    #the spend cap or the deadline stopped the first pass: ship the sections finished so far, ungraded
    def stop_early(self, reason):
        self.log(f"   Stopped early: {reason}")
        self.state.decide("pipeline", action = "stopped early", reason = reason)
        report = self.assemble()
        missing = [TITLES[section] for section in SECTION_OWNERS if not report.get(section) or report[section].get("claim") == ""]
        report["warnings"].append(f"Stopped early: {reason}" + (f" Not written: {', '.join(missing)}." if missing else ""))
        evaluation = {"passed": False, "failed": ["not evaluated"], "criteria": {}, "spot_checks": [], "rounds": 0, "stop_rule": reason}
        return self.finish(report, evaluation)

    #add the warnings, evaluation and usage to the report, and save it with the run
    def finish(self, report, evaluation):
        warnings = list(report["warnings"])  #from the stage outputs this report was built from
        for cid, criterion in evaluation["criteria"].items():
            if not criterion["passed"]:
                score = f" ({criterion['score']}/5)" if criterion["check"] == "model" else ""
                warnings.append(f"Failed {cid} {criterion['name']}{score}: {criterion['feedback']}")
        if not evaluation["passed"]:
            warnings.append(f"The report did not pass the rubric (stopped because {evaluation['stop_rule']}).")

        state = self.state
        report["warnings"] = warnings
        report["evaluation"] = evaluation
        report["usage"] = {
            "cost_usd": round(state.spent, 4),
            "cap_usd": state.cap_usd,
            "seconds": round(state.seconds(), 1),
            "run_id": state.run_id,
            "cost_by_stage": {stage: round(cost, 4) for stage, cost in state.cost_by_stage.items()},
        }
        state.status = "passed" if evaluation["passed"] else "shipped with warnings"
        state.finish("evaluator")
        if state.folder:
            with open(os.path.join(state.folder, "report.json"), "w") as f:
                json.dump(report, f, indent = 2, ensure_ascii = False)
        return report


#run the whole pipeline on one name and return the report
def run_pipeline(raw_name, **options):
    return Pipeline(raw_name, **options).run()

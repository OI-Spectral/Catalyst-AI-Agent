#run the eval set and compare the pipeline with the one-call baseline.
#
#   python evals/run_evals.py --stage route              gate + router only (cheap: a few cents in all)
#   python evals/run_evals.py                            the full pipeline on every case
#   python evals/run_evals.py --compare baseline         ...and the baseline, graded by the same evaluator
#   python evals/run_evals.py --only Kant,Nitzsche       just the cases whose input contains these
#   python evals/run_evals.py --jobs 4                   run four cases at a time (their progress lines interleave)
#
#Results are saved to runs/evals-<time>.json, with the comparison table in runs/evals-<time>.md.
import os
import re
import sys
import json
import time
import argparse
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#read .env before the other modules read their settings from the environment
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from pipeline import Pipeline, GateStopped
from baseline import baseline_report
from evaluator import evaluate
from state import RunState, RUNS_DIR

CASES_FILE = os.path.join(ROOT, "evals", "cases.jsonl")


#names compared without accents, case or punctuation; "Al-Ghazali" matches "Abu Hamid al-Ghazali"
def same_name(expected, actual):
    def words(name):
        name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
        return set(re.findall(r"[a-z]+", name))
    return bool(words(expected)) and words(expected) <= words(actual)


#citations the evaluator spot-checked, and how many the grader said the page supports
def citation_support(evaluation):
    checks = evaluation.get("spot_checks", [])
    return sum(check["supports"] == "yes" for check in checks), len(checks)


def summary_of(report, evaluation, cost, seconds):
    supported, checked = citation_support(evaluation)
    return {
        "passed": evaluation["passed"], "failed": evaluation["failed"], "rounds": evaluation.get("rounds", 0),
        "stop_rule": evaluation.get("stop_rule"), "weighted_score": report.get("panel", {}).get("weighted_score"),
        "theory": report.get("key_theory", {}).get("name"), "citations_supported": supported, "citations_checked": checked,
        "cost_usd": round(cost, 4), "seconds": round(seconds, 1),
    }


def run_case(case, stage, compare_baseline):
    result = {"input": case["input"], "category": case.get("category"), "checks": {}}
    pipeline = Pipeline(case["input"])
    try:
        try:
            pipeline.gate()
            result["gate"], result["name"] = "valid", pipeline.name
        except GateStopped as e:
            result["gate"], result["reason"] = e.outcome, e.message
        expected = case.get("expect_gate")
        result["checks"]["gate"] = expected is None or result["gate"] == expected
        if case.get("expect_name") and result["gate"] == "valid":
            result["checks"]["name"] = same_name(case["expect_name"], result["name"])

        if result["gate"] == "valid" and stage in ("route", "full"):
            pipeline.route()
            result["route"] = pipeline.route_name
            if case.get("expect_route"):
                result["checks"]["route"] = result["route"] == case["expect_route"]

        if result["gate"] == "valid" and stage == "full":
            report = pipeline.build_report()
            result["pipeline"] = summary_of(report, report["evaluation"], pipeline.state.spent, pipeline.state.seconds())
            result["run_id"] = pipeline.state.run_id  #runs/<run_id>/report.json holds the full report

            if compare_baseline:
                state = RunState(f"baseline {pipeline.name}")
                started = time.monotonic()
                report, allowlist = baseline_report(pipeline.name, pipeline.settings, pipeline.routes, pipeline.client, state)
                cost, seconds = state.spent, time.monotonic() - started
                #grading is measurement, so its cost is kept apart from the baseline's
                grading = RunState(f"grade baseline {pipeline.name}")
                evaluation = evaluate(report, allowlist, pipeline.rubric, pipeline.settings, pipeline.client, grading)
                result["baseline"] = {**summary_of(report, evaluation, cost, seconds), "grading_cost_usd": round(grading.spent, 4)}
    except Exception as e:  #one broken case shouldn't stop the rest
        result["error"] = f"{type(e).__name__}: {e}"
    result["run_cost_usd"] = round(pipeline.state.spent, 4)
    return result


#the results that have this part (pipeline or baseline)
def rows_with(results, key):
    return [result[key] for result in results if key in result]


#everything a case spent: the pipeline, plus the baseline and its grading if they ran
def total_cost(result):
    baseline = result.get("baseline", {})
    return result["run_cost_usd"] + baseline.get("cost_usd", 0) + baseline.get("grading_cost_usd", 0)


def comparison_table(results):
    lines = []
    gate = [r["checks"]["gate"] for r in results if "gate" in r["checks"]]
    names = [r["checks"]["name"] for r in results if "name" in r["checks"]]
    routes = [r["checks"]["route"] for r in results if "route" in r["checks"]]
    lines += [f"Gate as expected: {sum(gate)} / {len(gate)}",
              f"Name corrected as expected: {sum(names)} / {len(names)}",
              f"Route as expected: {sum(routes)} / {len(routes)}"]
    errors = [r["input"] for r in results if "error" in r]
    if errors:
        lines.append(f"Errors (not counted above): {len(errors)}: {', '.join(errors)}")
    lines.append("")

    columns = [("Baseline (one call)", rows_with(results, "baseline")), ("Full pipeline", rows_with(results, "pipeline"))]
    if not any(rows for _, rows in columns):
        return "\n".join(lines)

    def cell(rows, what):
        if not rows:
            return "–"
        if what == "pass":
            return f"{sum(row['passed'] for row in rows)} / {len(rows)}"
        if what == "citations":
            supported, checked = sum(row["citations_supported"] for row in rows), sum(row["citations_checked"] for row in rows)
            return f"{supported / checked:.0%} ({supported} / {checked})" if checked else "–"
        if what == "cost":
            return f"${sum(row['cost_usd'] for row in rows) / len(rows):.3f}"
        return f"{sum(row['seconds'] for row in rows) / len(rows):.0f} s"

    lines += ["| | " + " | ".join(name for name, _ in columns) + " |", "|---|---|---|"]
    for label, what in (("Rubric pass rate", "pass"), ("Citations that support their claim", "citations"),
                        ("Cost per run", "cost"), ("Latency", "latency")):
        lines.append(f"| {label} | " + " | ".join(cell(rows, what) for _, rows in columns) + " |")
    lines.append("\nThe baseline's cost leaves out the gate and the evaluator that graded it; the pipeline's includes both.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description = "Run the Philosopher Verdict eval set.")
    parser.add_argument("--stage", choices = ["gate", "route", "full"], default = "full", help = "how far to run each case")
    parser.add_argument("--compare", choices = ["baseline"], help = "also run and grade the one-call baseline")
    parser.add_argument("--only", help = "comma-separated: run only cases whose input contains one of these")
    parser.add_argument("--jobs", type = int, default = 1, help = "how many cases to run at once")
    args = parser.parse_args()

    with open(CASES_FILE) as f:
        cases = [json.loads(line) for line in f if line.strip()]
    if args.only:
        wanted = [word.strip().lower() for word in args.only.split(",")]
        cases = [case for case in cases if any(word in case["input"].lower() for word in wanted)]

    def run_one(numbered):
        n, case = numbered
        print(f"\n=== [{n}/{len(cases)}] {case['input']}")
        result = run_case(case, args.stage, args.compare == "baseline")
        failed = [check for check, ok in result["checks"].items() if not ok]
        if "error" in result:
            outcome = f"ERROR {result['error']}"
        else:
            outcome = "checks passed" if not failed else "FAILED " + ", ".join(failed)
        print(f"=== {case['input']}: gate {result.get('gate')}, route {result.get('route', '-')}, {outcome}")
        return result

    with ThreadPoolExecutor(max_workers = max(1, args.jobs)) as pool:
        results = list(pool.map(run_one, enumerate(cases, 1)))  #in the cases' order, however they finish

    table = comparison_table(results)
    print("\n" + table)
    os.makedirs(RUNS_DIR, exist_ok = True)
    path = os.path.join(RUNS_DIR, datetime.now().strftime("evals-%Y%m%d-%H%M%S"))
    with open(path + ".json", "w") as f:
        json.dump(results, f, indent = 2, ensure_ascii = False)
    with open(path + ".md", "w") as f:
        f.write(table + "\n")
    print(f"\nSaved {path}.json and {path}.md (total spend ${sum(total_cost(r) for r in results):.2f})")


if __name__ == "__main__":
    main()

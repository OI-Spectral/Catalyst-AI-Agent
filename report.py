import copy

from agent import url_key


#which stage writes each section; the evaluator sends a section's failures to its owner
SECTION_OWNERS = {
    "life": "research",
    "key_theory": "orchestrator",
    "case_for": "pro",
    "case_against": "con",
    "panel": "panel",
    "verdict": "aggregator",
}
TITLES = {"life": "Life", "key_theory": "Key theory", "case_for": "The case for", "case_against": "The case against",
          "panel": "Panel scores", "verdict": "Verdict"}


#the report's six sections and its sources. The baseline answers in exactly this shape, and the
#evaluator's R1 check holds every report to it.
def report_schema(criteria):
    def section(**fields):
        properties = {**fields, "sources": {"type": "array", "items": {"type": "string"}}}
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    text, texts = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
    argument = section(claim = text, premises = texts, conclusion = text, proponents = texts)
    score = section(
        criterion = {"type": "string", "enum": list(criteria)},
        weight = {"type": "number"},
        score = {"type": "integer", "enum": list(range(1, 11))},
        justification = text,
    )
    source = {
        "type": "object",
        "properties": {"id": text, "url": text, "excerpt": text, "claim": text},
        "required": ["id", "url", "excerpt", "claim"],
        "additionalProperties": False,
    }
    properties = {
        "life": section(text = text),
        "key_theory": section(name = text, statement = text, why_chosen = text),
        "case_for": argument,
        "case_against": argument,
        "panel": {
            "type": "object",
            "properties": {"scores": {"type": "array", "items": score}, "weighted_score": {"type": "number"}},
            "required": ["scores", "weighted_score"],
            "additionalProperties": False,
        },
        "verdict": {"type": "object", "properties": {"text": text}, "required": ["text"], "additionalProperties": False},
        "sources": {"type": "array", "items": source},
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


#problems with a value's shape against a JSON schema (types, required keys, enums); extra keys are allowed
def check_shape(value, schema, path = "report"):
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    kind = schema.get("type")
    if kind and (not isinstance(value, types[kind]) or (kind in ("integer", "number") and isinstance(value, bool))):
        return [f"{path} should be a {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} is not one of the allowed values"]
    problems = []
    if kind == "object":
        problems += [f"{path}.{key} is missing" for key in schema.get("required", []) if key not in value]
        for key, sub_schema in schema.get("properties", {}).items():
            if key in value:
                problems += check_shape(value[key], sub_schema, f"{path}.{key}")
    if kind == "array":
        for i, item in enumerate(value):
            problems += check_shape(item, schema.get("items", {}), f"{path}[{i}]")
    return problems


#a side whose worker never ran (the run stopped early)
EMPTY_ARGUMENT = {"claim": "", "premises": [], "conclusion": "", "proponents": [], "citations": []}


#build the report from each stage's output. Findings keep their ids (F1...); the pro and con
#citations become P1... and C1.... Returns a copy, so later revisions can't change a report already graded.
#Until the orchestrator has run, the theory is the research agent's pick.
def assemble(philosopher, route, dossier, plan, arguments, panel = None, verdict = None):
    active = [f for f in dossier["findings"] if f["status"] == "active"]
    active_ids = {f["id"] for f in active}
    sources = [{"id": f["id"], "url": f["source_url"], "excerpt": f["excerpt"], "claim": f["claim"]} for f in active]
    theory = plan["theory"] if plan else dossier["key_theory"]
    report = {
        "philosopher": philosopher,
        "route": route,
        "life": {"text": dossier["biography"]["text"], "sources": [i for i in dossier["biography"]["finding_ids"] if i in active_ids]},
        "key_theory": {key: theory[key] for key in ("name", "statement", "why_chosen")},
    }
    report["key_theory"]["sources"] = [i for i in theory["finding_ids"] if i in active_ids]  #a discounted finding is cited nowhere
    for side, section, prefix in (("pro", "case_for", "P"), ("con", "case_against", "C")):
        argument = arguments.get(side, EMPTY_ARGUMENT)
        ids = [f"{prefix}{n}" for n in range(1, len(argument["citations"]) + 1)]
        sources += [
            {"id": source_id, "url": citation["source_url"], "excerpt": citation["excerpt"], "claim": argument["claim"]}
            for source_id, citation in zip(ids, argument["citations"])
        ]
        report[section] = {key: argument[key] for key in ("claim", "premises", "conclusion", "proponents")}
        report[section]["sources"] = ids
    if panel:
        report["panel"] = panel
    if verdict:
        report["verdict"] = verdict
    report["sources"] = sources
    report["warnings"] = list(dossier.get("warnings", [])) + [
        f"{side} argument: {note}" for side in ("pro", "con")
        for note in arguments.get(side, {}).get("warnings", []) + arguments.get(side, {}).get("problems", [])
    ]
    return copy.deepcopy(report)


def cites(ids):
    return f" [{', '.join(ids)}]" if ids else ""


#the report as plain text for a model to read (judges, verdict writer, evaluator), leaving out some sections
def report_text(report, leave_out = ()):
    lines = [f"Philosopher: {report.get('philosopher')} (route: {report.get('route')})"]
    life, theory = report.get("life"), report.get("key_theory")
    if life and "life" not in leave_out:
        lines += ["", "LIFE", life["text"] + cites(life["sources"])]
    if theory and "key_theory" not in leave_out:
        lines += ["", f"KEY THEORY: {theory['name']}", theory["statement"] + cites(theory["sources"]),
                  f"Why this theory: {theory['why_chosen']}"]
    for section in ("case_for", "case_against"):
        argument = report.get(section)
        if argument and section not in leave_out:
            lines += ["", TITLES[section].upper(), f"Claim: {argument['claim']}", "Premises:"]
            lines += [f"{n}. {premise}" for n, premise in enumerate(argument["premises"], 1)]
            lines += [f"Conclusion: {argument['conclusion']}",
                      f"Proponents: {', '.join(argument['proponents']) or 'none named'}",
                      "Sources:" + (cites(argument["sources"]) or " none")]
    panel = report.get("panel")
    if panel and "panel" not in leave_out:
        lines += ["", "PANEL SCORES"]
        lines += [f"- {s['criterion']} (weight {s['weight']:.0%}): {s['score']}/10. {s['justification']}{cites(s['sources'])}"
                  for s in panel["scores"]]
        lines.append(f"Weighted score: {panel['weighted_score']} / 10")
    verdict = report.get("verdict")
    if verdict and "verdict" not in leave_out:
        lines += ["", "VERDICT", verdict["text"]]
    lines += ["", "SOURCES"]
    lines += [f"[{s['id']}] {s['url']}\n    cited for: {s['claim']}\n    excerpt: \"{s['excerpt']}\"" for s in report.get("sources", [])]
    return "\n".join(lines)


#the sources the essay cites, one per page, numbered in the order the essay first cites them.
#Returns the pages ({number, url, excerpts}) and each source id's number.
def numbered_sources(report):
    by_id = {source["id"]: source for source in report.get("sources", [])}
    cited = []
    for section in ("life", "key_theory", "case_for", "case_against"):
        cited += (report.get(section) or {}).get("sources", [])
    for score in (report.get("panel") or {}).get("scores", []):
        cited += score["sources"]
    pages, numbers = {}, {}
    for source_id in cited:
        if source_id not in by_id or source_id in numbers:
            continue
        source = by_id[source_id]
        page = pages.setdefault(url_key(source["url"]), {"number": len(pages) + 1, "url": source["url"], "excerpts": []})
        if source["excerpt"] not in page["excerpts"]:
            page["excerpts"].append(source["excerpt"])
        numbers[source_id] = page["number"]
    return list(pages.values()), numbers


def marks(ids, numbers):
    found = sorted({numbers[i] for i in ids if i in numbers})
    return f" [{', '.join(map(str, found))}]" if found else ""


#the essay and its sources in Markdown, for the terminal when there is no Google Doc, and the Streamlit demo
def render_markdown(report):
    pages, numbers = numbered_sources(report)
    theory, panel = report["key_theory"], report.get("panel") or {"scores": [], "weighted_score": None}
    lines = [f"# {report['philosopher']}", ""]
    if panel["scores"]:
        lines += [f"*Best known for {theory['name']} · panel score {panel['weighted_score']} / 10*", ""]
    lines += ["## 1. Life", report["life"]["text"] + marks(report["life"]["sources"], numbers), "",
              f"## 2. Key theory: {theory['name']}", f"> {theory['statement']}{marks(theory['sources'], numbers)}", "",
              f"*Why this theory.* {theory['why_chosen']}"]
    for n, section in ((3, "case_for"), (4, "case_against")):
        argument = report[section]
        lines += ["", f"## {n}. {TITLES[section]}"]
        if not argument["claim"]:
            lines.append("*Not written: the run stopped early.*")
            continue
        lines += [argument["claim"], ""] + [f"{k}. {premise}" for k, premise in enumerate(argument["premises"], 1)]
        lines += ["", f"*Therefore,* {argument['conclusion']}{marks(argument['sources'], numbers)}"]
        if argument["proponents"]:
            lines += ["", f"*Defended by* {', '.join(argument['proponents'])}."]
    lines += ["", "## 5. Panel scores"]
    if not panel["scores"]:
        lines.append("*Not scored: the run stopped early.*")
    for score in panel["scores"]:
        lines.append(f"- **{score['criterion'].title()}** ({score['weight']:.0%}): {score['score']} / 10. "
                     f"{score['justification']}{marks(score['sources'], numbers)}")
    if panel["scores"]:
        lines += ["", f"**Weighted score: {panel['weighted_score']} / 10**"]
    verdict = (report.get("verdict") or {}).get("text") or "*Not written: the run stopped early.*"
    lines += ["", "## 6. Verdict", verdict, "", "## Sources"]
    for page in pages:
        lines.append(f"{page['number']}. <{page['url']}>")
        lines += [f"   > \u201c{excerpt}\u201d" for excerpt in page["excerpts"]]
    return "\n".join(lines)


#everything about how the run went, for the terminal: route, rubric, warnings, cost
def render_run_notes(report):
    lines = ["Run notes", f"  Route: {report.get('route')}"]
    evaluation, usage = report.get("evaluation"), report.get("usage")
    if evaluation:
        outcome = "passed the rubric" if evaluation["passed"] else "did not pass (" + ", ".join(evaluation["failed"]) + ")"
        lines.append(f"  Evaluator: {outcome} after {evaluation.get('rounds', 0)} revision round(s); stopped because {evaluation.get('stop_rule')}.")
        if evaluation.get("criteria"):
            lines.append("  Scores: " + " · ".join(f"{cid} {c['score']}" for cid, c in evaluation["criteria"].items()))
        checks = evaluation.get("spot_checks") or []
        if checks:
            lines.append(f"  Spot checks: {sum(c['supports'] == 'yes' for c in checks)} of {len(checks)} cited pages fully support their claim.")
    if report.get("warnings"):
        lines.append("  Warnings:")
        lines += [f"    - {warning}" for warning in report["warnings"]]
    if usage:
        lines.append(f"  Cost ${usage['cost_usd']:.2f} of ${usage['cap_usd']:.2f} · {usage['seconds']:.0f} s · runs/{usage['run_id']}/")
    return "\n".join(lines)


#head-to-head: two reports' panel scores side by side, compared in code
def compare(first, second):
    a = {s["criterion"]: s for s in first["panel"]["scores"]}
    b = {s["criterion"]: s for s in second["panel"]["scores"]}
    lines = [f"| Criterion | {first['philosopher']} | {second['philosopher']} |", "|---|---|---|"]
    for criterion in a:
        other = b[criterion]["score"] if criterion in b else "-"
        lines.append(f"| {criterion.title()} ({a[criterion]['weight']:.0%}) | {a[criterion]['score']} | {other} |")
    first_score, second_score = first["panel"]["weighted_score"], second["panel"]["weighted_score"]
    lines.append(f"| **Weighted** | **{first_score}** | **{second_score}** |")
    if first_score == second_score:
        lines.append(f"\nLevel on the weighted score ({first_score}).")
    else:
        leader = first if first_score > second_score else second
        lines.append(f"\n{leader['philosopher']} scores higher ({max(first_score, second_score)} vs {min(first_score, second_score)}). "
                     f"They were judged on different theories: {first['key_theory']['name']} and {second['key_theory']['name']}.")
    return "\n".join(lines)

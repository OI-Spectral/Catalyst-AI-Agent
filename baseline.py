from llm import ask_json
from report import report_schema


#the sites the baseline may cite: every route's allowlist together
def baseline_allowlist(routes):
    return sorted({domain for route in routes["routes"].values() for domain in route["allowlist"]})


def baseline_prompt(settings, allowlist):
    criteria = "; ".join(f"{name} ({spec['weight']:.0%}): {spec['question']}" for name, spec in settings["criteria"].items())
    return (
        "You write a complete Philosopher Verdict report on one philosopher, in one answer and without tools. "
        "Good means philosophically good: the quality and impact of the ideas, not the person's moral character.\n\n"
        "- life: a biography of at most 200 words.\n"
        "- key_theory: the idea they are best known for, stated precisely enough to argue for and against, and why you chose it.\n"
        "- case_for: the strongest published argument for that theory; case_against: the strongest published objection to it. "
        "Each has a claim, premises, a conclusion and the philosophers who defend it, in at most 250 words.\n"
        f"- panel: score the theory from 1 to 10 on each criterion, with a justification: {criteria}. "
        "weight is the criterion's weight as a fraction, and weighted_score is the weighted average of the scores, to one decimal place.\n"
        "- verdict: how good a philosopher they were, in at most 200 words. It follows from the scores and engages both arguments.\n"
        f"- sources: every source you cite, with an id (S1, S2, and so on), a URL on one of these sites: {', '.join(allowlist)}; "
        "an excerpt of under 40 words quoted word for word from that page; and the claim it supports. "
        "Every section, and every score, lists the ids of the sources it relies on."
    )


#the baseline: one well-prompted call that writes the whole report. The pipeline has to beat it to justify its cost.
def baseline_report(name, settings, routes, client, state):
    allowlist = baseline_allowlist(routes)
    report = ask_json(
        state.client_for(client, "baseline"), report_schema(list(settings["criteria"])), effort = "medium", max_tokens = 16000,
        model = settings["models"]["baseline"], system = baseline_prompt(settings, allowlist),
        messages = [{"role": "user", "content": f"Write the report on: {name}"}],
    )
    return {"philosopher": name, "route": "baseline", **report}, allowlist

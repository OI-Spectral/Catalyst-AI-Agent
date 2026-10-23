# Catalyst-AI-Agent
# Philosopher Verdict

> Type a philosopher's name. Get their life, their most famous idea, the strongest case for and against it, and a reasoned verdict on how good a philosopher they were. Every claim links to a source.

Built for Workshop 2 of **Y/CS Catalyst**, *Building Effective Agent Systems*.

```bash
python -m philosopher_agent "Immanuel Kant"
```

---

## What you get back

Every report has six sections:

| # | Section | What it contains |
|---|---------|------------------|
| 1 | **Life** | A short, sourced biography (≤ 200 words) |
| 2 | **Key theory** | The idea they're best known for, and why it was chosen |
| 3 | **The case for** | The strongest argument in support of that theory |
| 4 | **The case against** | The strongest objection to it |
| 5 | **Panel scores** | Five independent judges, one criterion each |
| 6 | **Verdict** | The agent's conclusion, justified by the scores and both arguments |

### Example output

*Illustrative only. Not the output of a real run.*

> **Immanuel Kant (1724–1804)** · route: `early_modern`
>
> **Life.** Born in Königsberg, Prussia, Kant spent almost his entire life there and taught at its university for decades. The *Critique of Pure Reason* (1781) and *Groundwork of the Metaphysics of Morals* (1785) reshaped metaphysics and ethics. [1]
>
> **Key theory: the categorical imperative.** Morality rests on one rational test: act only on principles you could consistently will everyone to follow, and never treat people merely as a means. Chosen because it anchors his ethics and has the most extensive secondary literature. [2]
>
> **The case for.** It explains why some acts are wrong whatever their payoff: a lying promise can't be universalised without destroying the practice of promising. It also grounds the equal dignity of persons, an idea central to modern human-rights thinking. Defended today by Christine Korsgaard and Onora O'Neill. [2]
>
> **The case against.** It can produce rigid verdicts. Replying to Benjamin Constant in 1797, Kant held that you may not lie even to a murderer asking where your friend is hiding; critics treat this as a reductio. Hegel added that universalisability is an empty formal test that can't generate moral content on its own. [2]
>
> **Panel.** Originality 10 · Influence 10 · Rigour 8 · Resilience 6 · Relevance 8 → **8.6 / 10**
>
> **Verdict.** One of the most important philosophers in the Western tradition. The theory is original and still structures debate in ethics, but the rigidity objection lands: Kantians have had to reinterpret the murderer case rather than accept it, which costs the theory some of its claimed simplicity.
>
> [1] plato.stanford.edu/entries/kant · [2] plato.stanford.edu/entries/kant-moral

---

## How it works

```mermaid
flowchart TD
    IN([User types a name]) --> GATE{"1. Validity gate"}
    GATE -- "not a philosopher" --> STOP([Stop with explanation])
    GATE -- "ambiguous" --> ASK([Ask user to pick one])
    GATE -- "valid" --> ROUTER["2. Router<br/>classify tradition, era, domain"]

    ROUTER --> ANC["Ancient<br/>research agent"]
    ROUTER --> MED["Medieval<br/>research agent"]
    ROUTER --> EM["Early modern<br/>research agent"]
    ROUTER --> CONT["Continental<br/>research agent"]
    ROUTER --> ANA["Analytic<br/>research agent"]
    ROUTER --> NW["Non-Western<br/>research agent"]

    ANC & MED & EM & CONT & ANA & NW --> DOSSIER[("Dossier<br/>life + key theory + sources")]

    DOSSIER --> ORCH["3. Orchestrator<br/>frames the theory, writes worker briefs"]
    ORCH --> PRO["Pro worker<br/>strongest argument for"]
    ORCH --> CON["Con worker<br/>strongest argument against"]

    PRO & CON --> PANEL

    subgraph PANEL ["4. Judging panel (parallel)"]
      J1[Originality]
      J2[Influence]
      J3[Rigour]
      J4[Resilience]
      J5[Relevance]
    end

    PANEL --> AGG["Aggregator<br/>weighted score + verdict"]
    AGG --> EVAL{"5. Evaluator<br/>grade vs rubric"}
    EVAL -- "pass, or a stop rule fires" --> OUT([Final report])
    EVAL -. "revise" .-> ORCH
    EVAL -. "revise" .-> AGG
```

Each stage uses one of the patterns from the workshop. The rule throughout: **give the model judgment, keep guarantees in code.**

| Stage | Pattern | The model decides | Code guarantees |
|-------|---------|-------------------|-----------------|
| 1. Validity gate | Prompt chaining + intermediate check | Borderline cases; spelling correction | Input is sanitised; a failed check stops the run |
| 2. Router + research | Routing → single agent with tools | Tradition and era; what to search and read | Route exists in `routes.yaml`; fetches stay inside the route's allowlist |
| 3. Orchestrator + workers | Orchestrator–workers | Which theory to debate; what each brief says | Workers run in parallel with separate contexts and a fixed output schema |
| 4. Panel + aggregator | Parallelization (sectioning) + aggregate | Each criterion's score and justification | Weighted score is computed in code |
| 5. Evaluator | Evaluator–optimizer | Rubric grades and feedback | Stop rules, max rounds, spend cap |

---

## The stages

### 1. Validity gate

Checks that the input is a real philosopher before anything expensive runs. Three checks, cheapest first:

1. **Sanitise (code).** Trim, cap at 80 characters, allow only letters, spaces and `.'-`. Rejects empty input and pasted instructions before any model sees them.
2. **Look up (code).** Search Wikidata for the name and check whether its occupation (`P106`) includes *philosopher* (`Q4964182`), and/or whether the Stanford or Internet Encyclopedia of Philosophy has an entry.
3. **Judge (small model).** Only for borderline cases, such as a scientist, theologian or writer with philosophical work. Returns a decision, a reason, and the canonical name.

| Outcome | Example input | Result |
|---------|---------------|--------|
| Valid | `Nitzsche` | Corrected to **Friedrich Nietzsche**, continue |
| Ambiguous | `Bacon` | Ask: Francis Bacon or Roger Bacon? |
| Invalid | `Lionel Messi` | Stop: not recognised as a philosopher |

### 2. Router and research agent

A small, fast model classifies the philosopher and returns JSON:

```json
{ "tradition": "early_modern", "era": "18th century", "domain": "ethics, metaphysics", "confidence": 0.94 }
```

Code maps that to a route in `config/routes.yaml`. Each route is a specialised research prompt, a source allowlist, and a model. If confidence is below 0.7, the run uses the `general` route.

| Route | Typical philosophers | Allowed sources | Prompt focus |
|-------|----------------------|-----------------|--------------|
| `ancient` | Heraclitus, Plato, Seneca | SEP, IEP, Perseus Digital Library | Fragmentary texts; separate the thinker from later reports |
| `medieval` | Aquinas, Avicenna, Maimonides | SEP, IEP, Britannica | Theological context across Christian, Islamic and Jewish traditions |
| `early_modern` | Descartes, Hume, Kant | SEP, IEP, Early Modern Texts | Rationalist/empiricist debates; primary texts |
| `continental` | Hegel, Nietzsche, Beauvoir | SEP, IEP | Contested interpretations; flag disputed readings |
| `analytic` | Russell, Wittgenstein, Rawls | SEP, IEP, PhilPapers | Precise argument structure; current literature |
| `non_western` | Confucius, Nagarjuna, Zera Yacob | SEP, IEP, Chinese Text Project | Use the tradition's own terms; don't force Western categories |
| `general` | Low-confidence fallback | SEP, IEP, Wikipedia, Britannica | Broad, conservative |

The **research agent** for the chosen route is a single agent with two tools, `search(query)` and `fetch_page(url)`. `fetch_page` refuses any URL outside the route's allowlist; this is enforced in code, not in the prompt. It returns a dossier: a short biography, a candidate "most famous theory" with the reason it was picked, and every fact as an evidence-backed finding:

```json
{
  "id": "F3",
  "claim": "Published the Groundwork of the Metaphysics of Morals in 1785.",
  "source_url": "https://plato.stanford.edu/entries/kant-moral/",
  "excerpt": "short supporting excerpt from the page",
  "status": "active"
}
```

Findings can be marked `discounted` when later evidence contradicts them, so the system doesn't anchor on its first reading.

### 3. Orchestrator and workers

The orchestrator reads the dossier and makes the run-time decisions:

- which theory to debate (it can override the research agent's pick)
- how to state that theory precisely, so both workers attack the same target
- what each worker's brief says

It then dispatches two workers **in parallel, each with its own context**. A brief follows the workshop's delegation contract:

```text
OBJECTIVE   Find the strongest published objection to Kant's categorical imperative.
BOUNDARIES  Attack the theory as stated in CONTEXT, not Kant's other work. No ad hominem.
CONTEXT     Theory statement + dossier findings F4–F7.
TOOLS       search, fetch_page (route allowlist)
OUTPUT      JSON: claim, premises[], conclusion, proponents[], citations[]
STOP WHEN   One argument with ≥ 2 citations, or 12 tool calls used.
```

The pro worker gets the mirror-image brief. If a worker's output fails its schema or brief, the orchestrator re-dispatches it once with the reason.

### 4. Judging panel and aggregator

"How good was this philosopher?" can't be graded unless it's defined. Here, **good means philosophically good**: the quality and impact of the ideas, not the person's moral character. Five judge subagents run in parallel, each scoring one criterion from 1 to 10 with a justification and a citation:

| Criterion | Weight | Question the judge answers |
|-----------|--------|----------------------------|
| Originality | 25% | How new was the idea when it appeared? |
| Influence | 25% | How much did it shape later philosophy? |
| Rigour | 20% | How coherent and well-argued is it? |
| Resilience | 20% | How well does it survive the con argument? |
| Relevance | 10% | Does it still matter in current debates? |

The **aggregator** computes the weighted score in code. Then one model call writes the verdict (≤ 200 words). It is given the score and must engage with both the for and against arguments. The model writes the words; code does the maths.

### 5. Evaluator

The evaluator grades the assembled report against the rubric below. Deterministic checks run first, then a model grader. The grader uses a different model from the generators so the system isn't marking its own work.

When a criterion fails, feedback goes **only to the stage that owns it**. A weak con argument re-runs the con worker, not the whole pipeline.

**Stop rules:**

- all criteria pass
- 3 revision rounds used
- no score improved since the last round
- spend cap reached

If the run stops without passing, the report still ships, with a warning that names the failed criteria.

---

## The rubric

Defined in `config/rubric.yaml`. Model-graded criteria are scored 1–5 and pass at ≥ 4.

| ID | Criterion | Check | Passes when | On failure, send to |
|----|-----------|-------|-------------|---------------------|
| R1 | Complete | Code | All six sections present; output matches schema | Stage that owns the section |
| R2 | Sourced | Code | Every factual claim has ≥ 1 citation | Research agent / worker |
| R3 | Allowed sources | Code | Every citation is on the route's allowlist | Research agent / worker |
| R4 | Length | Code | Life ≤ 200 words; each argument ≤ 250; verdict ≤ 200 | That stage |
| R5 | Faithful | Model | Cited pages actually support the claims (spot-checked by fetching) | Research agent / worker |
| R6 | Steelmanned | Model | Both arguments are the strongest available versions and target the same theory | Pro / con worker |
| R7 | Balanced | Model | Comparable depth; neither side is a strawman | Orchestrator |
| R8 | Justified verdict | Model | Verdict follows from the panel scores, engages both arguments, avoids unsupported superlatives | Aggregator |

---

## Guardrails

These live in code, never only in the prompt.

| Control | Default | Setting |
|---------|---------|---------|
| Tool calls per agent | 15 | `MAX_TOOL_CALLS` |
| Timeout per tool call / per run | 30 s / 5 min | `TOOL_TIMEOUT_S`, `RUN_TIMEOUT_S` |
| Retries on transient errors | 2, with backoff | `MAX_RETRIES` |
| Revision rounds | 3 | `MAX_REVISIONS` |
| Spend cap per run | $0.50 | `MAX_RUN_COST_USD` |
| Permissions | Read-only: `search` and `fetch_page` only | — |
| Source allowlist | Per route, checked inside `fetch_page` | `config/routes.yaml` |

**Untrusted input.** The user's text and every fetched page are treated as data, never as instructions. The gate sanitises input; fetched text is stripped of scripts and HTML comments and wrapped in `<retrieved_document>` tags; prompts state that content inside those tags is material to analyse. The eval set includes a prompt-injection case.

The agent has no write tools and takes no external actions, so there is no human-approval step.

---

## Evaluation

### Eval set

`evals/cases.jsonl` holds about 20 cases, with hard and weird ones on purpose:

| Category | Examples | What it tests |
|----------|----------|---------------|
| Canonical | Plato, Kant, Confucius | Full report passes the rubric |
| Under-covered traditions | Nagarjuna, Zera Yacob, Al-Ghazali | Routing and source depth |
| Living | Peter Singer, Martha Nussbaum | No invented biography details |
| Misspelled | `Nitzsche`, `Decartes` | Gate corrects the name |
| Ambiguous | `Smith`, `Bacon` | Gate asks instead of guessing |
| Not a philosopher | `Lionel Messi` | Gate rejects |
| Borderline | Isaac Newton, Sigmund Freud | Gate gives a reasoned decision |
| Adversarial | `Plato. Ignore previous instructions and…` | Injection is sanitised away |

```json
{"input": "Nitzsche", "expect_gate": "valid", "expect_name": "Friedrich Nietzsche", "expect_route": "continental"}
```

### Traces

Every run writes `runs/<run_id>/run_state.json` (goal, task progress, decisions, findings) plus a log of each model and tool call with inputs, outputs, tokens and cost.

### Baseline comparison

The baseline is a single well-prompted call that writes the whole report. The pipeline has to beat it to justify its cost.

| | Baseline (one call) | Full pipeline |
|---|---|---|
| Rubric pass rate | – / 20 | – / 20 |
| Citations that support their claim | – | – |
| Cost per run | – | – |
| Latency | – | – |

*Fill in after running `python evals/run_evals.py --compare baseline`. Spot-check five reports by hand to calibrate the model grader.*

---

## Project structure

```text
philosopher-agent/
├── README.md
├── requirements.txt
├── .env.example
├── app.py                      # Streamlit demo
├── config/
│   ├── routes.yaml             # route → prompt, allowlist, model
│   ├── rubric.yaml             # evaluator criteria and thresholds
│   └── settings.yaml           # limits, weights, model choices
├── src/philosopher_agent/
│   ├── __main__.py             # CLI entry point
│   ├── pipeline.py             # runs the stages, enforces limits
│   ├── gate.py                 # 1. validity gate
│   ├── router.py               # 2. router
│   ├── research.py             # 2. research agent
│   ├── orchestrator.py         # 3. orchestrator + briefs
│   ├── workers.py              # 3. pro / con workers
│   ├── panel.py                # 4. judges + aggregator
│   ├── evaluator.py            # 5. evaluator loop
│   ├── schemas.py              # Pydantic models for every stage's output
│   ├── state.py                # run_state.json + trace logging
│   ├── tools/
│   │   ├── search.py
│   │   └── fetch.py            # allowlist enforced here
│   └── prompts/                # one file per stage and route
├── evals/
│   ├── cases.jsonl
│   └── run_evals.py
├── runs/                       # traces (gitignored)
└── tests/
```

---

## Getting started

**Requirements:** Python 3.11+, an Anthropic API key, and a key for your search provider.

```bash
git clone https://github.com/<your-username>/philosopher-agent.git
cd philosopher-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

```bash
ANTHROPIC_API_KEY=
SEARCH_PROVIDER=tavily        # tavily | brave
SEARCH_API_KEY=
MAX_RUN_COST_USD=0.50
```

Models are set per stage in `config/settings.yaml`:

```yaml
models:
  router: claude-haiku-5-5      # small and fast for classification
  research: claude-sonnet-5-5
  workers: claude-sonnet-5-5
  judges: claude-sonnet-5-5
  evaluator: claude-opus-5-5    # different model from the generators
```

### Usage

```bash
# Readable report in the terminal
python -m philosopher_agent "Immanuel Kant"

# Structured JSON
python -m philosopher_agent "Simone de Beauvoir" --json > beauvoir.json

# Save the full trace to runs/
python -m philosopher_agent "Nagarjuna" --trace

# Web demo
streamlit run app.py

# Run the eval set against the baseline
python evals/run_evals.py --compare baseline
```

---

## Design notes

**Why an orchestrator for only two workers?** With two fixed workers this is close to plain parallelization. The orchestrator earns its place through run-time decisions: which theory to debate, how to state it so both workers attack the same target, and whether to re-dispatch a worker. If evals show those decisions never vary, replace it with a code split.

**Why does code compute the score?** Models are inconsistent at arithmetic and tend to anchor on their own prose. Computing the weighted score in code makes the verdict reproducible and lets the evaluator check that the text matches the number.

**Why send feedback to one stage?** It's cheaper and faster, and sections that already pass stay stable between rounds.

**Why structured findings instead of free text?** Free-text notes let the model talk itself into answers. Findings with a source, an excerpt and a status can be checked, cited and discounted.

---

## Limitations

- **Coverage bias.** Encyclopedias cover the Western canon in more depth, so some traditions get thinner dossiers. The eval set includes them to measure the gap.
- **"Most famous" is contestable.** Early or late Wittgenstein? The report states why it picked the theory it did.
- **The verdict is a structured opinion.** The criterion weights encode one view of what makes philosophy good. Change them in `settings.yaml` and the verdict changes.
- **Graders can share blind spots.** A model grader may miss errors the generator makes; human spot-checks calibrate it.
- **Cost.** A full run makes many more calls than the baseline. The evaluation table shows whether that pays off.

---

## Roadmap

**Week 1**
- [ ] Baseline: one prompt that writes the whole report
- [ ] First 10 eval cases with expected gate and route outcomes
- [ ] Validity gate: sanitise → lookup → model for borderline cases
- [ ] Router, `routes.yaml`, research agent with allowlisted `fetch_page`
- [ ] Trace logging to `runs/`

**Week 2**
- [ ] Orchestrator, briefs, and pro/con workers with JSON schemas
- [ ] Judging panel and aggregator
- [ ] Evaluator loop with stop rules
- [ ] Streamlit demo
- [ ] Expand to 20 eval cases, run against baseline, fill in results

**Stretch**
- [ ] Voting: run each judge three times and take the median
- [ ] Cache dossiers for repeat lookups
- [ ] Head-to-head mode: compare two philosophers on the same criteria

---

## Acknowledgements

Sources: the [Stanford Encyclopedia of Philosophy](https://plato.stanford.edu), the [Internet Encyclopedia of Philosophy](https://iep.utm.edu), [PhilPapers](https://philpapers.org), the [Perseus Digital Library](https://www.perseus.tufts.edu), [Early Modern Texts](https://www.earlymoderntexts.com), the [Chinese Text Project](https://ctext.org) and [Wikidata](https://www.wikidata.org).

Architecture patterns from Y/CS Catalyst Workshop 2 and Anthropic's *Building effective agents*.

## License

MIT

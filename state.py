import os
import re
import json
import time
import threading
import unicodedata
from datetime import datetime
from types import SimpleNamespace

import anthropic

from costs import response_cost


#guardrails (see the README's Guardrails table)
MAX_RUN_COST_USD = float(os.getenv("MAX_RUN_COST_USD", "5.00"))
RUN_TIMEOUT_S = float(os.getenv("RUN_TIMEOUT_S", "900"))
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
CALL_GRACE_S = 60       #how far past the run's deadline a model call already under way may go
MIN_CALL_TIMEOUT_S = 60 #every model call gets at least this long


#the run hit a limit and must stop starting new work; the pipeline ships what it has.
#Not a RuntimeError, so code that retries a failed stage never retries this.
class RunStopped(Exception):
    pass


#raised instead of making a model call once the run has used its spend cap
class BudgetExceeded(RunStopped):
    pass


#the run's deadline (RUN_TIMEOUT_S) has passed
class TimeUp(RunStopped):
    pass


#a name made safe for a folder name: "Zera Yacob" -> "zera-yacob"
def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


#a response block as plain JSON for the trace, without the long thinking signatures
def dump_block(block):
    data = block.model_dump(mode = "json", by_alias = True, exclude_none = True) if hasattr(block, "model_dump") else block
    if isinstance(data, dict):
        data.pop("signature", None)
    return data


#everything one run has done: spend, time, decisions, findings and a one-line log of every call.
#It is saved to runs/<run_id>/run_state.json; with trace on, each call's inputs and outputs also go to trace.jsonl.
class RunState:
    def __init__(self, goal, trace = False, save = True, cap_usd = None, timeout_s = None):
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + slug(goal)
        self.goal = goal
        self.cap_usd = MAX_RUN_COST_USD if cap_usd is None else cap_usd
        self.started = time.monotonic()
        self.deadline = self.started + (RUN_TIMEOUT_S if timeout_s is None else timeout_s)
        self.spent = 0.0
        self.reserved = 0.0   #held back for model calls still in flight
        self.model_calls = 0
        self.cost_by_stage = {}
        self.largest_call = {}  #the most one call at each stage has cost, to hold back for the next one
        self.progress = []    #stages finished, in order
        self.decisions = []   #run-time choices: route, theory, re-dispatches, revisions, why the run stopped
        self.findings = []
        self.calls = []
        self.status = "running"
        self.trace = trace
        self.folder = os.path.join(RUNS_DIR, self.run_id) if save else None
        self.lock = threading.Lock()  #workers and judges record their calls from several threads at once

    def seconds(self):
        return time.monotonic() - self.started

    def time_left(self):
        return self.deadline - time.monotonic()

    def budget_left(self):
        return self.cap_usd - self.spent

    #a client for one stage: it refuses calls once the cap is used and records what each call cost
    def client_for(self, client, stage):
        return TrackedClient(client, self, stage)

    def record_model_call(self, stage, request, response, seconds):
        cost = response_cost(response, request.get("model"))
        usage = response.usage
        line = {
            "stage": stage, "type": "model", "model": response.model, "stop_reason": response.stop_reason,
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_input_tokens or 0,
            "cost_usd": round(cost, 5), "seconds": round(seconds, 1),
        }
        with self.lock:
            self.spent += cost
            self.model_calls += 1
            self.cost_by_stage[stage] = self.cost_by_stage.get(stage, 0) + cost
            self.largest_call[stage] = max(cost, self.largest_call.get(stage, 0))
            self.calls.append(line)
        if self.trace:
            self.write_trace({
                **line,
                "system": request.get("system"),
                "input": request.get("messages", [None])[-1],  #the newest message; earlier ones are in earlier lines
                "output": [dump_block(block) for block in response.content],
            })

    def record_tool_call(self, stage, name, tool_input, output, seconds, error = False):
        line = {"stage": stage, "type": "tool", "tool": name, "input": tool_input, "error": error, "seconds": round(seconds, 1)}
        with self.lock:
            self.calls.append(line)
        if self.trace:
            self.write_trace({**line, "output": output})

    def decide(self, stage, **decision):
        with self.lock:
            self.decisions.append({"stage": stage, "at_s": round(self.seconds(), 1), **decision})

    def finish(self, stage):
        with self.lock:
            self.progress.append(stage)
        self.save()

    def write_trace(self, line):
        if not self.folder:
            return
        with self.lock:
            os.makedirs(self.folder, exist_ok = True)
            with open(os.path.join(self.folder, "trace.jsonl"), "a") as f:
                f.write(json.dumps(line, ensure_ascii = False, default = str) + "\n")

    def save(self):
        if not self.folder:
            return
        with self.lock:
            snapshot = {
                "run_id": self.run_id,
                "goal": self.goal,
                "status": self.status,
                "progress": list(self.progress),
                "decisions": list(self.decisions),
                "findings": list(self.findings),
                "spent_usd": round(self.spent, 4),
                "cap_usd": self.cap_usd,
                "cost_by_stage": {stage: round(cost, 4) for stage, cost in self.cost_by_stage.items()},
                "seconds": round(self.seconds(), 1),
                "calls": list(self.calls),
            }
            os.makedirs(self.folder, exist_ok = True)
            path = os.path.join(self.folder, "run_state.json")
            with open(path + ".tmp", "w") as f:
                json.dump(snapshot, f, indent = 2, ensure_ascii = False, default = str)
            os.replace(path + ".tmp", path)  #never leave a half-written file behind


#stands in for Anthropic() wherever a stage expects a client (client.beta.messages.create):
#it refuses a call once the run's spend cap is used, and records every call's cost in the run state
class TrackedClient:
    def __init__(self, client, state, stage):
        self.client = client
        self.state = state
        self.stage = stage
        self.beta = SimpleNamespace(messages = SimpleNamespace(create = self.create))

    def create(self, **request):
        state = self.state
        #while a call is in flight, hold back the most a call at this stage has cost (or an average call, for a
        #stage's first call), so a call that would probably take the run over the cap is refused, and calls
        #started together (the judges, the two workers) can't all slip under it at once
        with state.lock:
            average = state.spent / state.model_calls if state.model_calls else 0.0
            hold = state.largest_call.get(self.stage, average)
            if state.spent >= state.cap_usd or state.spent + state.reserved + hold > state.cap_usd:
                raise BudgetExceeded(f"The ${state.cap_usd:.2f} spend cap was used up before {self.stage} could run.")
            state.reserved += hold
        #no call may run much past the run's deadline: its timeout is the time left plus a grace period,
        #and once the deadline has passed a timed-out call isn't retried
        time_left = state.time_left()
        client = self.client.with_options(timeout = max(MIN_CALL_TIMEOUT_S, time_left + CALL_GRACE_S),
                                          **({} if time_left > 0 else {"max_retries": 0}))
        started = time.monotonic()
        try:
            response = client.beta.messages.create(**request)
        except anthropic.APITimeoutError as e:
            if state.time_left() <= 0:
                raise TimeUp(f"The run's {state.deadline - state.started:.0f} s limit passed during {self.stage}.") from e
            raise
        finally:
            with state.lock:
                state.reserved -= hold
        state.record_model_call(self.stage, request, response, time.monotonic() - started)
        return response

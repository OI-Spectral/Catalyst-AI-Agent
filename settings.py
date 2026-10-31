import os

import yaml


CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.yaml")
MODEL_STAGES = ("gate", "router", "orchestrator", "workers", "judges", "aggregator", "evaluator", "baseline")


#read settings.yaml and check it has everything the later stages need
def load_settings(path = SETTINGS_FILE):
    with open(path) as f:
        settings = yaml.safe_load(f) or {}

    models = settings.get("models") or {}
    for stage in MODEL_STAGES:
        if not models.get(stage):
            raise ValueError(f"settings.yaml: models.{stage} is missing.")
    #the grader must not mark its own work
    if models["evaluator"] in {models[stage] for stage in ("orchestrator", "workers", "judges", "aggregator")}:
        raise ValueError("settings.yaml: the evaluator must use a different model from the generators.")

    criteria = settings.get("criteria") or {}
    if not criteria:
        raise ValueError("settings.yaml has no criteria.")
    for name, criterion in criteria.items():
        weight = criterion.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0 or not criterion.get("question"):
            raise ValueError(f"settings.yaml: criterion '{name}' needs a positive weight and a question.")
    total = sum(criterion["weight"] for criterion in criteria.values())
    if abs(total - 1) > 1e-6:
        raise ValueError(f"settings.yaml: the criteria weights add up to {total:g}, not 1.")

    shares = settings.get("budget_shares") or {}
    for key in ("research", "worker"):
        if not isinstance(shares.get(key), (int, float)) or not 0 < shares[key] < 1:
            raise ValueError(f"settings.yaml: budget_shares.{key} must be between 0 and 1.")
    #each worker may use its share, plus half again if it is sent back; the rest of the run needs at least a fifth
    if shares["research"] + 3 * shares["worker"] > 0.8:
        raise ValueError("settings.yaml: the budget shares leave too little for the other stages.")

    for key, smallest in (("worker_max_tool_calls", 1), ("judge_votes", 1)):
        value = settings.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < smallest:
            raise ValueError(f"settings.yaml: {key} must be a whole number of at least {smallest}.")
    if settings["judge_votes"] % 2 == 0:
        raise ValueError("settings.yaml: judge_votes must be odd so the median is one of the votes.")
    settings["cache_dossiers"] = bool(settings.get("cache_dossiers"))
    return settings

import os
import sys
import json
import yaml
from anthropic import Anthropic
from llm import read_json
from settings import load_settings


ROUTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "routes.yaml")


#read routes.yaml and check every route has what the router and research agent need
def load_routes(path = ROUTES_FILE):
    with open(path) as f:
        config = yaml.safe_load(f) or {}

    routes = config.get("routes") or {}
    if not routes:
        raise ValueError("routes.yaml has no routes.")
    if config.get("fallback") not in routes:
        raise ValueError(f"routes.yaml: the fallback route '{config.get('fallback')}' is not defined.")
    if not isinstance(config.get("min_confidence"), (int, float)):
        raise ValueError("routes.yaml: min_confidence must be a number.")
    for label, route in routes.items():
        for key in ("description", "model", "allowlist", "prompt"):
            if not route.get(key):
                raise ValueError(f"routes.yaml: route '{label}' is missing '{key}'.")
    return config

#router to classify what type of philosopher the person is.
def philosopher_type_agent(name, config = None, client = None, model = None):

    #model and the way it presents its decision
    config = config or load_routes()
    MODEL = model or load_settings()["models"]["router"]  #small, fast model for classification, set in settings.yaml
    #labels come from routes.yaml; the fallback is left out because code picks it when confidence is low
    labels = [label for label in config["routes"] if label != config["fallback"]]
    DECISION_SCHEMA = {
        "type": "object",
        "properties": {
            "tradition": {"type": "string", "enum": labels},
            "era" : {"type": "string"},
            "domain" : {"type": "string"},
            "confidence": {"type": "number"},  #minimum/maximum aren't supported here, so pick_route checks the range
            },
        "required": ["tradition", "era", "domain", "confidence"],
        "additionalProperties": False,
        }

    #describe each route so the model knows what each label means
    route_list = []
    for label in labels:
        route = config["routes"][label]
        examples = ", ".join(route.get("examples") or [])
        route_list.append(f"- {label}: {route['description']}" + (f" (e.g. {examples})" if examples else ""))

    #ask the model for its decision in the fixed JSON shape above
    client = client or Anthropic()
    response = client.beta.messages.create(
        model = MODEL,
        max_tokens = 5000,
        betas = ["server-side-fallback-2026-07-01"],
        fallbacks = "default",  #if this model declines, the API retries on another
        system = (
            "Decide which type of philosopher the user is asking about, choosing one of these traditions:\n"
            + "\n".join(route_list) + "\n"
            "Decide which era the philosopher belongs to (e.g. 18th century) and what domain of philosophy (e.g. ethics or metaphysics) that they are associated with. "
            "The decision should be based on the name and any known historical context. "
            "Provide a confidence score between 0 and 1 for your decision. "
            "Give a low score if their work spans several traditions or you don't recognise them."
        ),
        messages = [{
            "role": "user",
            "content": f"User typed: {name}",
        }],
        output_config = {"effort": "low", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}}
    )

    #return the decision (read_json stops on a refusal or running out of tokens)
    return read_json(response, "router")

#code picks the route: an unknown label, or confidence below min_confidence, uses the fallback route
def pick_route(decision, config):
    label = decision.get("tradition")
    confidence = decision.get("confidence")
    is_number = isinstance(confidence, (int, float)) and not isinstance(confidence, bool)

    if label not in config["routes"] or not is_number or not config["min_confidence"] <= confidence <= 1:
        label = config["fallback"]
    return label, config["routes"][label]

#router to different research agents based on the type of philosopher: the model classifies,
#code picks the route. The pipeline then runs that route's research agent.
def router_agent(name, client, state, config = None, model = None):
    config = config or load_routes()
    decision = philosopher_type_agent(name, config, state.client_for(client, "router"), model)
    route_name, route = pick_route(decision, config)
    print(f"Route: {route_name} (router said {decision['tradition']}, confidence {decision['confidence']})")
    state.decide("router", route = route_name, **decision)
    return route_name, route, decision


#try the classifier on its own (cheap, no research): python router.py "Immanuel Kant"
if __name__ == "__main__":
    name = " ".join(sys.argv[1:]) or "Immanuel Kant"
    config = load_routes()
    decision = philosopher_type_agent(name, config)
    route_name, route = pick_route(decision, config)
    print(json.dumps(decision, indent = 2, ensure_ascii = False))
    print(f"route: {route_name} (model {route['model']}, sources {', '.join(route['allowlist'])})")

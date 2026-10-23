import requests
import os
import json
from anthropic import Anthropic


#check if the philosopher's name is valid
def sanitise(args):
    #trim leading and trailing whitespace
    name = args.strip()

    #check if the name is valid
    if name == "":
        raise ValueError("Name cannot be empty.")
    elif not all(char.isalpha() or char.isspace() or char in ".'-" for char in name):
        raise ValueError("Name must contain only letters, spaces and . ' -")
    elif len(name) > 80:
        raise ValueError("Name cannot exceed 80 characters.")
    else:
        return name

#search wikidata and check if the person is a philosopher
def name_database_search(name, limit = 5):

    #API inputs
    API = "https://www.wikidata.org/w/api.php"
    WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
    HEADERS = {"User-Agent": "Philosopher-Verdict/0.1 (contact: you@example.com)"}
    PHILOSOPHER = "Q4964182"

    #ask wikipedia for a spelling suggestion (e.g. "Decartes" -> "descartes")
    #it can also "correct" names that were already right (e.g. "Plato" -> "pluto"),
    #so search it alongside the typed name rather than instead of it
    try:
        suggestion = requests.get(WIKIPEDIA_API, headers = HEADERS, timeout = 10, params = {
            "action": "query",
            "list": "search",
            "srsearch": name,
            "srinfo": "suggestion",
            "srlimit": 1,
            "format": "json",
        }).json()["query"].get("searchinfo", {}).get("suggestion")
    except (requests.RequestException, KeyError):
        #the suggestion is optional, so carry on without it if wikipedia is down or rate-limiting
        suggestion = None

    queries = [name]
    if suggestion and suggestion.lower() != name.lower():
        queries.append(suggestion)

    #searching wikidata for the philosopher, remembering which search found each match
    search_matches = {}
    for query in queries:
        response = requests.get(API, headers = HEADERS, timeout = 10, params = {
            "action": "query",
            "list": "search",
            "srsearch": f"{query} haswbstatement:P106={PHILOSOPHER}",
            "srlimit": limit,
            "format": "json",
        })
        response.raise_for_status()  #clear error (e.g. 429 Too Many Requests) instead of a JSON error
        search = response.json()
        for hit in search["query"]["search"]:
            search_matches.setdefault(hit["title"], query)

    #checking if the search returned any philosopher matches
    if not search_matches:
        raise ValueError(f"No philosopher found with the name '{name}'.")

    else:
        #fetch ids and their descriptions
        response = requests.get(API, headers=HEADERS, timeout=10, params={
            "action": "wbgetentities",
            "ids": "|".join(search_matches),
            "props": "labels|descriptions",
            "languages": "en",
            "format": "json",
        })
        response.raise_for_status()
        entities = response.json()["entities"]

        #return these search results
        return [
            {
                "id": query_id,
                "label": entities[query_id].get("labels", {}).get("en", {}).get("value"),
                "description": entities[query_id].get("descriptions", {}).get("en", {}).get("value"),
                "found_by_search": search_matches[query_id],
            }
            for query_id in search_matches
        ]

#final decision on potential ambiguity
def philosopher_judge_agent(name, candidates, client = None):

    #judge model inputs
    MODEL = os.getenv("JUDGE_MODEL", "claude-opus-5-5")
    DECISION_SCHEMA = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["valid", "ambiguous", "invalid"]},
            "reason": {"type": "string"},
            "canonical_name": {"type": "string"},
            },
        "required": ["decision", "reason", "canonical_name", "options"],
        "additionalProperties": False,
    }

    #ask the model for a decision in the fixed JSON shape above
    client = client or Anthropic()
    response = client.beta.messages.create(
        model = MODEL,
        max_tokens = 4096,
        betas = ["server-side-fallback-2026-07-01"],
        fallbacks = "default",  #if this model declines, the API retries on another
        system = (
            "Decide whether the user's input refers to a philosopher, using the Wikidata candidates. "
            "Each candidate's found_by_search is either the typed name or Wikipedia's spelling suggestion; "
            "use the suggestion only if the typed name looks like a misspelling of it. "
            "valid: one clear philosopher; put their full name in canonical_name. "
            "ambiguous: several philosophers could match; list their full names in options, most likely first. "
            "invalid: not a philosopher; leave canonical_name empty. "
        ),
        messages = [{
            "role": "user",
            "content": f"User typed: {name}\n\nWikidata candidates:\n{json.dumps(candidates, indent = 2)}",
        }],
        output_config = {"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
    )

    #return the decision
    if response.stop_reason == "refusal":
        raise RuntimeError("The judge model declined to answer.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The judge ran out of tokens before finishing its answer.")

    #read the JSON answer and act on the decision
    text = next(block.text for block in response.content if block.type == "text")
    decision = json.loads(text)

    if decision["decision"] == "valid":
        print(decision["reason"])
        return decision["canonical_name"]
    elif decision["decision"] == "ambiguous":
        raise ValueError(f"Ambiguous name error. Reason: {decision['reason']}")
    elif decision["decision"] == "invalid":
        raise ValueError(f"Invalid philosopher name. Reason: {decision['reason']}")
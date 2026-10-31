import json


FALLBACK_BETA = "server-side-fallback-2026-07-01"


#the assistant turn to send back next time. After a fallback, a declined model's thinking and
#tool calls (anything before the last fallback block except text) are left out, as the API requires.
def content_to_echo(response):
    blocks = list(response.content)
    switches = [i for i, block in enumerate(blocks) if block.type == "fallback"]
    if not switches:
        return blocks
    return [block for block in blocks[:switches[-1]] if block.type == "text"] + blocks[switches[-1] + 1:]


#the text of the final answer, written by the model that answered
def answer_text(response):
    blocks = list(response.content)
    switches = [i for i, block in enumerate(blocks) if block.type == "fallback"]
    texts = [block.text for block in blocks[switches[-1] + 1 if switches else 0:] if block.type == "text"]
    return "".join(texts) if texts else None


#stop if the model refused or ran out of tokens
def check_stop(response, stage):
    if response.stop_reason == "refusal":
        raise RuntimeError(f"The {stage} model declined to answer.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"The {stage} model ran out of tokens before finishing its answer.")


#read the JSON answer from a finished response
def read_json(response, stage):
    check_stop(response, stage)
    text = answer_text(response)
    if text is None:
        raise RuntimeError(f"The {stage} model finished without writing an answer.")
    return json.loads(text)


#one call that must answer in a fixed JSON shape
def ask_json(client, schema, effort = "medium", max_tokens = 8000, **request):
    response = client.beta.messages.create(
        max_tokens = max_tokens,
        betas = [FALLBACK_BETA],
        fallbacks = "default",  #if this model declines, the API retries on another
        output_config = {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        **request,
    )
    return read_json(response, getattr(client, "stage", "model"))


#call fn, and once more if it fails with a refusal or a broken answer; a RunStopped (spend cap, deadline) is neither, so it is never retried
def with_one_retry(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, ValueError):
        return fn(*args, **kwargs)


#schema for an id that must be one of a known set; an empty set allows any string, and code checks it
def one_of(ids):
    ids = list(ids)
    return {"type": "string", "enum": ids} if ids else {"type": "string"}


def word_count(text):
    return len((text or "").split())

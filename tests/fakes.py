#test doubles: a fake web for requests, and a fake Claude server for the Anthropic client. No network, no spend.
import copy
import json
import threading
from types import SimpleNamespace

import httpx
import requests
from anthropic import Anthropic


def web_response(status, headers, body = b""):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers)
    response._content = body.encode("utf-8") if isinstance(body, str) else body
    return response


def html_page(text):
    return web_response(200, {"Content-Type": "text/html"}, f"<html><head><title>t</title></head><body><p>{text}</p></body></html>")


#replaces requests.request: serves the given url -> Response map and records every url asked for
class FakeWeb:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []
        self.lock = threading.Lock()

    def __call__(self, method, url, **kwargs):
        with self.lock:
            self.asked.append(url)
        assert kwargs.get("allow_redirects") is False, "redirects must be followed by hand"
        if url not in self.pages:
            return web_response(404, {"Content-Type": "text/html"})
        return copy.deepcopy(self.pages[url])


def tool_use(tool_id, name, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def text(answer):
    return {"type": "text", "text": json.dumps(answer)}


#a fake Claude server: reply(body) returns (content blocks, stop_reason); every request body is recorded
class FakeClaude:
    def __init__(self, reply, model = "claude-sonnet-5-5"):
        self.reply = reply
        self.model = model
        self.requests = []
        self.lock = threading.Lock()

    def handler(self, request):
        body = json.loads(request.content)
        with self.lock:
            self.requests.append(body)
            turn = len(self.requests)
        content, stop_reason = self.reply(body)
        return httpx.Response(200, json = {
            "id": f"msg_{turn}", "type": "message", "role": "assistant", "model": body.get("model", self.model),
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        })

    def client(self):
        return Anthropic(api_key = "fake", max_retries = 0, http_client = httpx.Client(transport = httpx.MockTransport(self.handler)))

    #requests whose structured-output schema has this property
    def calls_with(self, prop):
        return [body for body in self.requests if prop in schema_properties(body)]


def schema_properties(body):
    return ((body.get("output_config") or {}).get("format") or {}).get("schema", {}).get("properties", {})


def first_message(body):
    content = body["messages"][0]["content"]
    return content if isinstance(content, str) else json.dumps(content)


def usage(input_tokens = 1000, output_tokens = 100, iterations = None):
    return SimpleNamespace(input_tokens = input_tokens, output_tokens = output_tokens, cache_creation_input_tokens = 0,
                           cache_read_input_tokens = 0, iterations = iterations)

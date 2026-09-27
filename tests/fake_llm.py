"""A fake LLM provider (OpenAI-compatible and Anthropic wire formats) for offline tests.

It reads the facts it was sent, cites only evidence IDs that appear in them, upholds every
objection it is allowed to, and can be told to misbehave (rate-limit, invent evidence IDs).
"""
import json
import re

import httpx


class FakeProvider:
    def __init__(self, fail_first: int = 0, invent_ids: bool = False) -> None:
        self.fail_first = fail_first
        self.invent_ids = invent_ids
        self.requests: list[dict] = []

    def answer(self, system: str, user: str) -> dict:
        ids = sorted(set(re.findall(r"EV-[A-Z]+-\d{3}", user)))
        accounts = sorted(set(re.findall(r"ACC_\d{2}", user)))
        if "SMS" in system:
            return {"message": "We have paused outgoing payments on your account for your safety. Please call us."}
        cited = ids[:4] + (["EV-XXX-999"] if self.invent_ids else [])
        body = {"summary": f"[fake-llm] Accounts {', '.join(accounts[:3])} examined; see {', '.join(cited)}.",
                "cited_evidence": cited}
        if "Role: JUDGE" in system:
            facts = json.loads(user.split("\n\nYour previous")[0])
            body["rulings"] = {a: ("upheld" if ok else "rejected") for a, ok in facts["independent_of_ring"].items()}
        return body

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": payload})
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate limited"})
        if request.url.path.endswith("/v1/messages"):
            out = self.answer(payload["system"], payload["messages"][-1]["content"])
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(out)}],
                                             "usage": {"input_tokens": 100, "output_tokens": 40}})
        msgs = payload["messages"]
        out = self.answer(msgs[0]["content"], msgs[-1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 40}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

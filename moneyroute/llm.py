"""Thin client for local models served by Ollama. Nothing is sent to a cloud LLM."""

import json
import re
import time

import requests

from moneyroute.config import policy


class LLMError(RuntimeError):
    pass


class CallCounter:
    """Counts LLM calls and latency per role so budgets can be enforced and reported."""

    def __init__(self):
        self.calls: list[dict] = []

    def record(self, role: str, model: str, seconds: float):
        self.calls.append({"role": role, "model": model, "seconds": round(seconds, 2)})

    def count(self, role_prefix: str = "") -> int:
        return sum(1 for c in self.calls if c["role"].startswith(role_prefix))


COUNTER = CallCounter()


def chat(messages: list[dict], *, role: str, model: str | None = None, schema: dict | None = None,
         temperature: float = 0.0, seed: int = 7, timeout: int = 300) -> str:
    cfg = policy()["models"]
    model = model or cfg["agent"]
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "seed": seed, "num_ctx": 8192},
    }
    if schema:
        body["format"] = schema
    start = time.time()
    response, last_exc = None, None
    for _ in range(2):  # one retry on transport errors or 5xx
        try:
            response = requests.post(f"{cfg['ollama_url']}/api/chat", json=body, timeout=min(timeout, 150))
            if response.status_code < 500:
                break
        except requests.RequestException as exc:
            last_exc = exc
            response = None
    if response is None:
        raise LLMError(f"Ollama call failed for {model}: {last_exc}") from last_exc
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LLMError(f"Ollama call failed for {model}: {exc}") from exc
    COUNTER.record(role, model, time.time() - start)
    return response.json()["message"]["content"].strip()


def chat_json(messages: list[dict], *, role: str, schema: dict | None, **kwargs) -> dict:
    """Structured output. With a schema, Ollama constrains decoding; without one (models that do not
    support it, such as Gemma 4 here) we parse the first JSON object in the reply."""
    text = chat(messages, role=role, schema=schema, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        raise LLMError(f"{role} returned invalid JSON: {text[:200]}")


def embed(texts: list[str]) -> list[list[float]]:
    cfg = policy()["models"]
    response = requests.post(f"{cfg['ollama_url']}/api/embed", json={"model": cfg["embed"], "input": texts}, timeout=300)
    response.raise_for_status()
    return response.json()["embeddings"]


def available() -> bool:
    try:
        requests.get(f"{policy()['models']['ollama_url']}/api/tags", timeout=3).raise_for_status()
        return True
    except requests.RequestException:
        return False

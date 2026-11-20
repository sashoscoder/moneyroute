"""Local audit log. One redacted JSON line per gate or agent step."""

import json
import time
import uuid

from moneyroute.config import runtime_dir
from moneyroute.gateway import redact


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def log(trace_id: str, agent: str, event: str, **fields) -> dict:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "trace": trace_id, "agent": agent, "event": event, **fields}
    line, _ = redact(json.dumps(record, default=str))
    with open(runtime_dir("logs") / f"audit-{time.strftime('%Y-%m-%d')}.jsonl", "a") as fh:
        fh.write(line + "\n")
    return record


def read(trace_id: str) -> list[dict]:
    out = []
    for f in sorted(runtime_dir("logs").glob("audit-*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("trace") == trace_id:
                out.append(rec)
    return out

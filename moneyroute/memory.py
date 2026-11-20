"""Long-term memory: semantic, episodic and procedural records.

Memory is local, holds only privacy-filtered data, and is written only through
commit(), which the graph calls after the user approves items on the approval
card. Every record carries its source, date and scope.
"""

import json
from datetime import date
from pathlib import Path

from moneyroute.config import runtime_dir


def path_for(profile_id: str) -> Path:
    return runtime_dir("memory") / f"{profile_id}.json"


def seed(profile_id: str, spec: dict, reset: bool = False) -> dict:
    """Initial confirmed facts, as if entered by the user during onboarding."""
    p = path_for(profile_id)
    if p.exists() and not reset:
        return load(profile_id)
    today = date.today().isoformat()
    mem = {
        "semantic": {
            "age_band": {"value": spec["age_band"], "source": "user", "date": today},
            "goal": {"value": spec["goal"], "source": "user", "date": today},
            "emergency_min": {"value": spec["emergency_min"], "source": "user", "date": today},
            "keep": {"value": list(spec.get("keep", [])), "source": "user", "date": today},
        },
        "episodic": [],
        "procedural": [],
    }
    p.write_text(json.dumps(mem, indent=2))
    return mem


def load(profile_id: str) -> dict:
    return json.loads(path_for(profile_id).read_text())


def facts(profile_id: str) -> dict:
    sem = load(profile_id)["semantic"]
    return {k: v["value"] for k, v in sem.items()}


def search(profile_id: str, topic: str) -> list[dict]:
    """Return only records relevant to a topic, never the whole store."""
    mem = load(profile_id)
    topic = topic.lower()
    hits = [{"type": "semantic", "key": k, **v} for k, v in mem["semantic"].items() if k in topic or topic in k]
    hits += [{"type": "procedural", **r} for r in mem["procedural"] if topic in json.dumps(r).lower()]
    hits += [{"type": "episodic", **r} for r in mem["episodic"][-3:] if topic in json.dumps(r).lower()]
    return hits


def commit(profile_id: str, approved: list[dict], month: str) -> list[dict]:
    """Write approved items. Called only after the approval interrupt resumes."""
    mem = load(profile_id)
    today = date.today().isoformat()
    written = []
    for item in approved:
        kind = item.get("memory")
        if kind == "episodic":
            rec = {"month": month, "decision": item["summary"], "data": item.get("data"), "source": "user-approved", "date": today}
            mem["episodic"].append(rec)
        elif kind == "procedural":
            rec = {"rule": item["summary"], "data": item.get("data"), "source": "user-confirmed", "date": today}
            mem["procedural"].append(rec)
        elif kind == "semantic":
            key = item["data"]["key"]
            rec = {"value": item["data"]["value"], "source": "user-confirmed", "date": today}
            mem["semantic"][key] = rec
        else:
            continue
        written.append({"kind": kind, **rec})
    path_for(profile_id).write_text(json.dumps(mem, indent=2))
    return written


def forget(profile_id: str) -> None:
    path_for(profile_id).unlink(missing_ok=True)

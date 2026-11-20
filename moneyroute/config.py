"""Paths and the static policy file."""

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNTIME = Path(os.environ.get("MONEYROUTE_RUNTIME", ROOT / "runtime"))
POLICY_FILE = ROOT / "config" / "moneyroute-policy.yaml"


@lru_cache
def policy() -> dict:
    cfg = yaml.safe_load(POLICY_FILE.read_text())
    if os.environ.get("MONEYROUTE_MODE"):
        cfg["mode"] = os.environ["MONEYROUTE_MODE"]
    return cfg


def runtime_dir(*parts: str) -> Path:
    path = RUNTIME.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path

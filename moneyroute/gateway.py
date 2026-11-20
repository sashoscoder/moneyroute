"""G1 input gate: the privacy gateway between the bank feed and everything else.

Non-LLM code. It keeps only allowlisted fields, replaces account numbers with
aliases, redacts PII from free text, and scans memo text for prompt-injection
patterns. Flagged memos are removed and recorded as security events. Raw
statements never reach prompts, memory, or logs.
"""

import re
import sqlite3
from pathlib import Path

ALLOWED_FIELDS = ("transaction_id", "date", "amount", "merchant_name", "category_hint", "memo", "tax_year")

PII_PATTERNS = [
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("account", re.compile(r"\b(?:[A-Z]{2,4})?\d{8,12}\b")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("phone", re.compile(r"(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]

INJECTION_PATTERNS = [
    r"\bsystem\s*:", r"\bassistant\s*:", r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |your )?(rules|instructions|policy)", r"you are now", r"new instructions",
    r"(user|account holder|owner) (has )?(already )?(approved|authori[sz]ed|agreed|consented)",
    r"(move|transfer|withdraw|liquidate|send) (the |all |my )?.{0,30}(emergency|fund|savings|balance|money)",
    r"<\|?im_start\|?>|\[inst\]|<</?sys>>", r"do not (tell|inform|ask) the user", r"override",
]


def redact(text: str) -> tuple[str, list[str]]:
    found = []
    for kind, pattern in PII_PATTERNS:
        if pattern.search(text or ""):
            found.append(kind)
            text = pattern.sub(f"[{kind.upper()}]", text)
    return text, found


def injection_signals(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [p for p in INJECTION_PATTERNS if re.search(p, lowered)]


def wrap_untrusted(text: str) -> str:
    return f"<untrusted>{text}</untrusted>"


def build_ledger(feed: dict, db_path: Path) -> dict:
    """Apply G1 to a raw feed and write the privacy-filtered ledger. Returns a gate report."""
    aliases = {a["account_number"]: a["name"] for a in feed["accounts"]}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    events, pii_count = [], 0
    for acct in feed["accounts"]:
        conn.execute("INSERT INTO accounts VALUES (?,?,?,?)",
                     (aliases[acct["account_number"]], acct["type"], acct["opening_balance"], acct["reported_balance"]))
    for raw in feed["transactions"]:
        t = {k: raw.get(k) for k in ALLOWED_FIELDS}
        memo, pii = redact(t["memo"] or "")
        merchant, pii_m = redact(t["merchant_name"] or "")
        pii_count += len(pii) + len(pii_m)
        signals = injection_signals(t["memo"] or "")
        if signals:
            events.append({"kind": "injection", "transaction_id": t["transaction_id"], "date": t["date"],
                           "merchant": merchant, "signals": len(signals), "excerpt": memo[:80]})
            memo = "[removed: possible instruction in memo]"
        is_transfer = 1 if t["category_hint"] == "Transfer" else 0
        conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)", (
            t["transaction_id"], t["date"], aliases[raw["account_number"]], t["amount"], merchant,
            t["category_hint"], memo, t["tax_year"], is_transfer))
    for e in events:
        conn.execute("INSERT INTO security_events VALUES (?,?,?,?,?)",
                     (e["transaction_id"], e["date"], e["kind"], e["signals"], e["excerpt"]))
    conn.commit()
    conn.close()
    return {"gate": "G1", "transactions": len(feed["transactions"]), "pii_redactions": pii_count,
            "injection_events": events, "accounts_aliased": len(aliases)}


SCHEMA = """
CREATE TABLE accounts (alias TEXT PRIMARY KEY, type TEXT, opening_balance REAL, reported_balance REAL);
CREATE TABLE transactions (id TEXT PRIMARY KEY, date TEXT, account TEXT, amount REAL, merchant TEXT,
                           category TEXT, memo TEXT, tax_year INTEGER, is_transfer INTEGER);
CREATE TABLE security_events (transaction_id TEXT, date TEXT, kind TEXT, signals INTEGER, excerpt TEXT);

CREATE VIEW txn AS
  SELECT id, date, substr(date, 1, 7) AS month, account, amount, merchant, category, is_transfer, tax_year
  FROM transactions;
CREATE VIEW monthly_spend AS
  SELECT substr(date, 1, 7) AS month, category, ROUND(-SUM(amount), 2) AS spend, COUNT(*) AS n
  FROM transactions WHERE is_transfer = 0 AND category NOT IN ('Income') AND amount < 0
  GROUP BY 1, 2;
CREATE VIEW balances AS
  SELECT alias AS account, type, reported_balance AS balance FROM accounts;
CREATE VIEW contributions AS
  SELECT tax_year, account, ROUND(SUM(amount), 2) AS amount FROM transactions
  WHERE account = 'IRA' AND amount > 0 GROUP BY 1, 2;
"""

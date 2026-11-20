"""Read-only ledger access (G3) and deterministic ledger tools.

The ledger is opened with SQLite mode=ro, and an authorizer allows only SELECT
reads of the approved views. Agent-written SQL must also pass a static check:
one SELECT statement, approved views only.
"""

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from moneyroute.config import policy
from moneyroute.synth import add_months, month_list

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|create|alter|replace|attach|detach|pragma|vacuum|reindex)\b", re.I)


class SQLRejected(ValueError):
    pass


def validate_sql(sql: str) -> str:
    """Static G3 check for agent-written SQL."""
    views = set(policy()["access"]["views"])
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt:
        raise SQLRejected("more than one statement")
    if not re.match(r"(?is)^\s*(with\b.*?\)\s*)?select\b", stmt):
        raise SQLRejected("only SELECT is allowed")
    if FORBIDDEN.search(stmt):
        raise SQLRejected("forbidden keyword")
    tables = {t.lower() for t in re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", stmt)}
    ctes = {c.lower() for c in re.findall(r"(?i)\b([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", stmt)}
    bad = tables - views - ctes
    if bad:
        raise SQLRejected(f"table not allowed: {sorted(bad)}")
    return stmt


class Ledger:
    def __init__(self, db_path: Path):
        self.path = db_path
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.views = set(policy()["access"]["views"])
        self.queries: list[dict] = []

    def _authorizer(self, action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # arg1 = table; source = view the read happens inside (None for direct reads)
            if arg1 in self.views or (source in self.views):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    def query(self, sql: str, params: tuple = (), *, agent_written: bool = False) -> list[dict]:
        if agent_written:
            sql = validate_sql(sql)
        self.conn.set_authorizer(self._authorizer)
        try:
            rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        finally:
            self.conn.set_authorizer(None)
        qid = f"Q{len(self.queries) + 1}"
        self.queries.append({"id": qid, "sql": " ".join(sql.split()), "rows": len(rows)})
        return rows

    @property
    def last_qid(self) -> str:
        return self.queries[-1]["id"] if self.queries else "Q0"

    # ---- deterministic tools -------------------------------------------------

    def spending_summary(self, month: str) -> dict:
        rows = self.query("SELECT category, spend FROM monthly_spend WHERE month = ?", (month,))
        by_cat = {r["category"]: r["spend"] for r in rows}
        income = self.query("SELECT ROUND(SUM(amount),2) AS v FROM txn WHERE month = ? AND category = 'Income'", (month,))[0]["v"] or 0
        total = round(sum(by_cat.values()), 2)
        return {"month": month, "by_category": by_cat, "total_spending": total, "income": income, "qid": self.last_qid}

    def category_averages(self, month: str, n: int = 6) -> dict:
        """Average spend per category over the n months before this check-in."""
        months = month_list(add_months(month, -1), n)
        rows = self.query(
            f"SELECT category, ROUND(SUM(spend)/{n}, 2) AS avg FROM monthly_spend WHERE month IN ({','.join('?' * n)}) GROUP BY category",
            tuple(months))
        return {r["category"]: r["avg"] for r in rows}

    def detect_recurring(self, month: str) -> dict:
        months = month_list(month, 6)
        rows = self.query(
            f"SELECT month, merchant, account, amount FROM txn WHERE category = 'Subscriptions' AND month IN ({','.join('?' * 6)})",
            tuple(months))
        series = defaultdict(dict)
        accounts = defaultdict(set)
        counts = defaultdict(lambda: defaultdict(int))
        for r in rows:
            series[r["merchant"]][r["month"]] = -r["amount"]
            accounts[r["merchant"]].add(r["account"])
            counts[r["merchant"]][r["month"]] += 1
        recurring, increases, duplicates = [], [], []
        prev = months[-2]
        for merchant, by_month in series.items():
            if len(by_month) < 3:
                continue
            current = by_month.get(month)
            previous = by_month.get(prev)
            recurring.append({"merchant": merchant, "price": current if current is not None else previous})
            if current is not None and previous is not None and round(current - previous, 2) > 0:
                increases.append({"merchant": merchant, "old": previous, "new": current,
                                  "monthly": round(current - previous, 2), "annual": round((current - previous) * 12, 2)})
            if counts[merchant].get(month, 0) > 1 or len(accounts[merchant]) > 1:
                duplicates.append({"merchant": merchant, "accounts": sorted(accounts[merchant])})
        # Duplicate rows that appeared only this month on another account
        dup_rows = self.query(
            "SELECT merchant, COUNT(*) AS n, COUNT(DISTINCT account) AS accts FROM txn WHERE category='Subscriptions' AND month = ? GROUP BY merchant HAVING n > 1",
            (month,))
        for r in dup_rows:
            if not any(d["merchant"] == r["merchant"] for d in duplicates):
                duplicates.append({"merchant": r["merchant"], "accounts": r["accts"]})
        return {"recurring": sorted(recurring, key=lambda x: -x["price"]), "increases": increases,
                "duplicates": duplicates, "qid": self.last_qid}

    def match_transfers(self, month: str) -> dict:
        """Pair transfer legs across the user's own accounts; unmatched legs get a low confidence."""
        rows = self.query("SELECT id, date, account, amount, merchant FROM txn WHERE is_transfer = 1 AND month = ?", (month,))
        unmatched = {r["id"]: r for r in rows}
        pairs = []
        for r in rows:
            if r["id"] not in unmatched or r["amount"] >= 0:
                continue
            for s in rows:
                if s["id"] in unmatched and s["account"] != r["account"] and abs(s["amount"] + r["amount"]) < 0.005 \
                        and abs(int(s["date"][-2:]) - int(r["date"][-2:])) <= 3:
                    pairs.append({"out": r["id"], "in": s["id"], "amount": -r["amount"], "confidence": 0.97})
                    unmatched.pop(r["id"]); unmatched.pop(s["id"])
                    break
        questions = []
        for r in unmatched.values():
            p2p = bool(re.search(r"venmo|zelle|cash ?app|paypal", r["merchant"], re.I))
            questions.append({"id": r["id"], "date": r["date"], "account": r["account"], "amount": r["amount"],
                              "merchant": r["merchant"], "confidence": 0.40 if p2p else 0.60,
                              "question": f"Is the {r['merchant']} {'deposit' if r['amount'] > 0 else 'payment'} of ${abs(r['amount']):,.2f} on {r['date']} income, a reimbursement, or a transfer between your own accounts?"})
        return {"matched": len(pairs), "matched_amount": round(sum(p["amount"] for p in pairs), 2),
                "unmatched": questions, "qid": self.last_qid}

    def reconcile(self) -> dict:
        rows = self.query(
            "SELECT b.account, b.balance FROM balances b")
        # Opening balances are not exposed as a view; reconciliation runs as a trusted tool.
        opening = {r["alias"]: r["opening_balance"] for r in self.conn.execute("SELECT alias, opening_balance FROM accounts")}
        sums = {r["account"]: r["s"] for r in self.query("SELECT account, SUM(amount) AS s FROM txn GROUP BY account")}
        diffs = []
        for r in rows:
            computed = round(opening[r["account"]] + (sums.get(r["account"]) or 0), 2)
            diff = round(r["balance"] - computed, 2)
            if abs(diff) > policy()["bounds"]["totals_tolerance"]:
                diffs.append({"account": r["account"], "reported": r["balance"], "computed": computed, "difference": diff})
        return {"ok": not diffs, "differences": diffs, "qid": self.last_qid}

    def balances(self) -> dict:
        return {r["account"]: r["balance"] for r in self.query("SELECT account, balance FROM balances")}

    def goal_transfer_avg(self, month: str, n: int = 3) -> float:
        months = month_list(month, n)
        rows = self.query(
            f"SELECT ROUND(SUM(amount)/{n}, 2) AS v FROM txn WHERE account = 'SAVINGS_GOAL' AND amount > 0 AND month IN ({','.join('?' * n)})",
            tuple(months))
        return rows[0]["v"] or 0.0

    def ira_monthly(self, month: str) -> float:
        rows = self.query("SELECT amount FROM txn WHERE account = 'IRA' AND amount > 0 AND month = ?", (month,))
        return rows[0]["amount"] if rows else 0.0

    def contributions(self, tax_year: int) -> float:
        rows = self.query("SELECT SUM(amount) AS v FROM contributions WHERE tax_year = ?", (tax_year,))
        return round(rows[0]["v"] or 0.0, 2)

    def security_events(self) -> list[dict]:
        # Trusted tool: the security_events table is never exposed to agent-written SQL.
        return [dict(r) for r in self.conn.execute("SELECT * FROM security_events")]

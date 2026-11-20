"""Synthetic bank feed generator and independent answer key.

The feed imitates what an aggregator such as Plaid Sandbox returns: raw account
numbers, merchant names, free-text memos and reported balances. The answer key
is computed from the profile specification, not from MoneyRoute's tools, so the
evaluation compares the agent against an independent source of truth.
"""

import copy
import math
from datetime import date

import yaml

from moneyroute.config import DATA

ACCOUNTS = {
    "CHECKING": {"number": "000123456789", "institution": "First Synthetic Bank", "type": "checking"},
    "CARD_A": {"number": "4111111111111234", "institution": "Synthetic Card Co", "type": "credit"},
    "CARD_B": {"number": "5500000000005678", "institution": "Example Credit Union", "type": "credit"},
    "SAVINGS_GOAL": {"number": "000987654321", "institution": "Online Synthetic Savings", "type": "savings"},
    "EMERGENCY": {"number": "000555000111", "institution": "Online Synthetic Savings", "type": "savings"},
    "IRA": {"number": "IRA00042424242", "institution": "Synthetic Brokerage", "type": "retirement"},
}
CATEGORY_CARD = {"Dining": "CARD_A", "Groceries": "CARD_A", "Shopping": "CARD_B", "Entertainment": "CARD_B", "Transport": "CARD_A"}
MERCHANTS = {
    "Dining": ["Corner Bistro", "Noodle House", "Cafe Lumen", "Taco Spot"],
    "Groceries": ["FreshMart", "Green Grocer"],
    "Shopping": ["ShopRight Online", "Home Goods Co"],
    "Entertainment": ["CinemaPlex", "Bowl-O-Rama"],
    "Transport": ["City Transit", "FuelUp"],
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_profile(profile_id: str = "alex", overrides: dict | None = None) -> dict:
    profiles = yaml.safe_load((DATA / "profiles.yaml").read_text())
    base = profiles.get(profile_id) or profiles["alex"]
    spec = deep_merge(base, overrides or {})
    spec["id"] = profile_id
    return spec


def month_list(end: str, n: int = 12) -> list[str]:
    y, m = map(int, end.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def months_between(start: str, end: str) -> int:
    """Inclusive count of months from start to end (YYYY-MM)."""
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    return (ey - sy) * 12 + (em - sm) + 1


def add_months(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _split(total: float, parts: int, salt: int) -> list[float]:
    """Deterministic split of a monthly total into transactions that sum exactly to it."""
    weights = [1 + ((salt * 7 + i * 13) % 5) / 4 for i in range(parts)]
    raw = [total * w / sum(weights) for w in weights]
    cents = [round(x, 2) for x in raw[:-1]]
    cents.append(round(total - sum(cents), 2))
    return cents


def generate_feed(spec: dict) -> dict:
    months = month_list(spec["checkin_month"])
    txns: list[dict] = []
    tid = 0

    def add(day_month: str, day: int, account: str, amount: float, merchant: str, category: str, memo: str = "", **extra):
        nonlocal tid
        tid += 1
        txns.append({
            "transaction_id": f"tx-{tid:05d}",
            "date": f"{day_month}-{day:02d}",
            "account_number": ACCOUNTS[account]["number"],
            "amount": round(amount, 2),
            "merchant_name": merchant,
            "category_hint": category,
            "memo": memo,
            **extra,
        })

    for mi, month in enumerate(months):
        year = int(month[:4])
        card_totals = {"CARD_A": 0.0, "CARD_B": 0.0}
        add(month, 1, "CHECKING", spec["income_paycheck"], "ACME Corp Payroll", "Income", "Direct deposit")
        add(month, 15, "CHECKING", spec["income_paycheck"], "ACME Corp Payroll", "Income", "Direct deposit")
        add(month, 1, "CHECKING", -spec["rent"], "Oakview Apartments", "Rent", "Monthly rent")
        add(month, 10, "CHECKING", -spec["utilities"], "City Power & Water", "Utilities")
        for ci, (category, total) in enumerate(spec["categories"].items()):
            card = CATEGORY_CARD.get(category, "CARD_A")
            for j, amount in enumerate(_split(total, 4, mi + ci)):
                merchant = MERCHANTS.get(category, [category])[j % len(MERCHANTS.get(category, [category]))]
                add(month, 4 + j * 6, card, -amount, merchant, category)
                card_totals[card] += amount
        for si, sub in enumerate(spec["subscriptions"]):
            price = sub.get("new_price", sub["price"]) if month == spec["checkin_month"] else sub["price"]
            add(month, 5 + si, "CARD_B", -price, sub["merchant"], "Subscriptions", "Recurring charge")
            card_totals["CARD_B"] += price
        if spec.get("duplicate_charge") and month == spec["checkin_month"]:
            dup = spec["duplicate_charge"]
            add(month, 20, "CARD_A", -dup["price"], dup["merchant"], "Subscriptions", "Recurring charge")
            card_totals["CARD_A"] += dup["price"]
        add(month, 2, "CHECKING", -spec["goal_transfer"], "Transfer to Online Synthetic Savings", "Transfer", f"To acct {ACCOUNTS['SAVINGS_GOAL']['number']}")
        add(month, 2, "SAVINGS_GOAL", spec["goal_transfer"], "Transfer from First Synthetic Bank", "Transfer", f"From acct {ACCOUNTS['CHECKING']['number']}")
        ira = spec["ira_monthly"] + (spec.get("ira_lump_january", 0) if month.endswith("-01") else 0)
        if ira:
            add(month, 3, "CHECKING", -ira, "Transfer to Synthetic Brokerage IRA", "Transfer", "IRA contribution", tax_year=year)
            add(month, 3, "IRA", ira, "Contribution", "Transfer", "IRA contribution", tax_year=year)
        for card, total in card_totals.items():
            if total:
                add(month, 25, "CHECKING", -total, f"Payment to {ACCOUNTS[card]['institution']}", "Transfer", f"Card payment {ACCOUNTS[card]['number']}")
                add(month, 26, card, total, "Payment received - thank you", "Transfer")

    last = spec["checkin_month"]
    for event in spec.get("events", []):
        kind = event["kind"]
        if kind == "memo":
            add(last, event.get("day", 18), event.get("account", "CARD_B"), -event.get("amount", 12.00),
                event.get("merchant", "Corner Bistro"), event.get("category", "Dining"), event["memo"])
        elif kind == "ambiguous_transfer":
            add(last, 21, "CHECKING", event["amount"], event.get("merchant", "Venmo cashout"), "Transfer", event.get("memo", "Venmo"))
        elif kind == "external_transfer_out":
            add(last, 22, "CHECKING", -event["amount"], event.get("merchant", "Zelle payment"), "Transfer", event.get("memo", ""))

    balances = {
        "SAVINGS_GOAL": spec["goal_balance"],
        "EMERGENCY": spec["emergency_balance"],
        "IRA": spec.get("ira_balance", 18000),
        "CHECKING": spec.get("checking_balance", 4200),
        "CARD_A": 0.0,
        "CARD_B": 0.0,
    }
    openings = {key: round(balances[key] - sum(t["amount"] for t in txns if t["account_number"] == a["number"]), 2)
                for key, a in ACCOUNTS.items()}
    dropped = None
    if spec.get("missing_transaction"):
        # Aggregator drops one row, but the institution's reported balance still includes it.
        dropped = next(t for t in reversed(txns) if t["category_hint"] == "Shopping")
        txns.remove(dropped)
    accounts = [{"account_number": a["number"], "institution": a["institution"], "type": a["type"], "name": key,
                 "opening_balance": openings[key], "reported_balance": balances[key]} for key, a in ACCOUNTS.items()]
    return {"profile_id": spec["id"], "generated": date.today().isoformat(), "accounts": accounts,
            "transactions": sorted(txns, key=lambda t: (t["date"], t["transaction_id"])), "_dropped": dropped}


def answer_key(spec: dict) -> dict:
    """Independent ground truth computed from the specification."""
    month = spec["checkin_month"]
    start = add_months(month, 1)
    n = months_between(start, spec["goal"]["deadline"])
    forecast = spec["goal_balance"] + spec["goal_transfer"] * n
    gap = max(0.0, spec["goal"]["target"] - forecast)
    subs_now = {s["merchant"]: s.get("new_price", s["price"]) for s in spec["subscriptions"]}
    increases = {s["merchant"]: round(s["new_price"] - s["price"], 2) for s in spec["subscriptions"] if "new_price" in s}
    year = int(month[:4])
    ira_months = int(month[5:])
    ira_ytd = spec["ira_monthly"] * ira_months + spec.get("ira_lump_january", 0)
    limit = {2024: 7000, 2025: 7000, 2026: 7500}.get(year)
    if limit and spec["age_band"] != "under 50":
        limit = {2024: 8000, 2025: 8000, 2026: 8600}[year]
    month_spend = spec["rent"] + spec["utilities"] + sum(spec["categories"].values()) + sum(subs_now.values())
    if spec.get("duplicate_charge"):
        month_spend += spec["duplicate_charge"]["price"]
    month_spend += sum(e.get("amount", 12.00) for e in spec.get("events", []) if e["kind"] == "memo")
    keep = set(spec.get("keep", []))
    cancellable = sum(p for m, p in subs_now.items() if m not in keep)
    max_cuts = sum(spec["categories"].get(c, 0) * 0.25 for c in ("Dining", "Shopping", "Entertainment"))
    return {
        "forecast": round(forecast, 2),
        "months": n,
        "gap": round(gap, 2),
        "monthly_needed": round(gap / n, 2) if gap else 0.0,
        "on_track": gap == 0,
        "price_increases": increases,
        "ira_contributed": round(ira_ytd, 2),
        "ira_limit": limit,
        "ira_remaining": round(limit - ira_ytd, 2) if limit else None,
        "month_spending": round(month_spend, 2),
        "income": spec["income_paycheck"] * 2,
        "cut_only_capacity": round(cancellable + max_cuts, 2),
        "emergency_headroom": spec["emergency_balance"] - spec["emergency_min"],
        "category_avg": dict(spec["categories"]),
        "subscriptions": subs_now,
        "keep": sorted(keep),
    }


def ceil_cents(x: float) -> float:
    return math.ceil(x * 100) / 100

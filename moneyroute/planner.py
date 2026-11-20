"""Goal planner: Tree-of-Thought beam search over savings plans (Checkpoint 4.1).

The LLM generator proposes thoughts as typed JSON, code runs hard checks and
computes goal coverage and safety, an LLM critic scores sustainability, and
Python decides what is pruned. Scores: V = 0.5 G + 0.3 S + 0.2 R.
"""

import itertools
import json
import random
import sqlite3
import time

from moneyroute import llm
from moneyroute.config import policy, runtime_dir
from moneyroute.synth import add_months, months_between

STRATEGIES = {
    "cut_spending": {"levers": ["cancel_subscription", "cut_category"], "label": "Cut spending"},
    "redirect_retirement": {"levers": ["reduce_ira", "pause_ira"], "label": "Redirect IRA contributions"},
    "use_emergency_fund": {"levers": ["use_emergency_fund"], "label": "Use the emergency fund"},
    "cuts_and_retirement": {"levers": ["cancel_subscription", "cut_category", "reduce_ira"], "label": "Cuts plus a smaller IRA contribution"},
    "cuts_and_emergency_fund": {"levers": ["cancel_subscription", "cut_category", "use_emergency_fund"], "label": "Cuts plus part of the emergency fund"},
}
LEVER_TYPES = ["cancel_subscription", "cut_category", "reduce_ira", "pause_ira", "use_emergency_fund"]


# ---- deterministic tools: forecast_goal and check_constraints ----------------

def forecast_goal(ctx: dict, monthly_added: float = 0.0, lump: float = 0.0, start_month: str | None = None) -> float:
    start = start_month or ctx["start_month"]
    extra_months = max(0, months_between(start, ctx["deadline"])) if start <= ctx["deadline"] else 0
    return round(ctx["goal_balance"] + ctx["goal_transfer"] * ctx["months"] + monthly_added * extra_months + lump, 2)


def evaluate_levers(ctx: dict, levers: list[dict]) -> dict:
    """Hard checks plus the code-computed parts of the score (G and R)."""
    bounds = policy()["bounds"]
    subs = {s["merchant"].lower(): s for s in ctx["subscriptions"]}
    keep = {k.lower() for k in ctx["keep"]}
    violations, monthly, lump, penalty, seen = [], 0.0, 0.0, 0.0, set()
    touches = set()
    for lv in levers:
        t, target, amount = lv.get("type"), (lv.get("target") or "").strip(), lv.get("amount")
        key = (t, "" if t in ("reduce_ira", "pause_ira", "use_emergency_fund") else target.lower())
        if key in seen:
            violations.append(f"duplicate lever {t} {target}")
            continue
        seen.add(key)
        if t == "cancel_subscription":
            sub = subs.get(target.lower())
            if not sub:
                violations.append(f"unknown subscription '{target}'")
            elif target.lower() in keep:
                violations.append(f"cancels '{sub['merchant']}', which the user chose to keep")
            else:
                monthly += sub["price"]
        elif t == "cut_category":
            cat = next((c for c in ctx["category_avg"] if c.lower() == target.lower()), None)
            if not cat or cat not in bounds["discretionary"]:
                violations.append(f"'{target}' is not a discretionary category")
            elif not amount or amount <= 0:
                violations.append(f"cut to {target} needs a positive amount")
            elif amount > ctx["category_avg"][cat] * bounds["max_category_cut"] + 0.005:
                violations.append(f"cuts {cat} by ${amount:,.2f}, above the 25% cap of ${ctx['category_avg'][cat] * bounds['max_category_cut']:,.2f}")
            else:
                monthly += amount
        elif t == "reduce_ira":
            if not amount or amount <= 0 or amount > ctx["ira_monthly"]:
                violations.append(f"IRA reduction must be between $0 and ${ctx['ira_monthly']:,.2f}")
            else:
                monthly += amount
                penalty += 5 if amount >= ctx["ira_monthly"] else 2.5
                touches.add("retirement")
        elif t == "pause_ira":
            if ctx["ira_monthly"] <= 0:
                violations.append("no IRA contribution to pause")
            else:
                monthly += ctx["ira_monthly"]
                penalty += 5
                touches.add("retirement")
        elif t == "use_emergency_fund":
            if not amount or amount <= 0:
                violations.append("emergency-fund use needs a positive amount")
            elif ctx["emergency_balance"] - amount < ctx["emergency_min"]:
                violations.append(f"leaves the emergency fund at ${ctx['emergency_balance'] - amount:,.0f}, below the ${ctx['emergency_min']:,.0f} minimum")
            else:
                lump += amount
            penalty += 4
            touches.add("emergency_fund")
        else:
            violations.append(f"lever type '{t}' is not allowed")
    monthly = round(monthly, 2)
    if {"reduce_ira", "pause_ira"} <= {lv.get("type") for lv in levers}:
        violations.append("combines reduce_ira and pause_ira")
    covered = monthly * ctx["months"] + lump
    g = 10 * min(1.0, covered / ctx["gap"]) if ctx["gap"] else 10
    return {"monthly_added": monthly, "lump": lump, "coverage": round(covered, 2), "G": round(g, 2),
            "R": max(0.0, 10 - penalty), "violations": violations, "touches": sorted(touches),
            "transfer": round(ctx["goal_transfer"] + monthly, 2), "changes": len(levers)}


def strategy_capacity(ctx: dict, strategy: str) -> tuple[float, float]:
    """Upper bound on what a strategy can add by the deadline, and its best-case safety score."""
    cap = policy()["bounds"]["max_category_cut"]
    cuts = sum(s["price"] for s in ctx["subscriptions"] if s["merchant"].lower() not in {k.lower() for k in ctx["keep"]})
    cuts += sum(ctx["category_avg"].get(c, 0) * cap for c in policy()["bounds"]["discretionary"])
    if strategy == "cut_spending":
        return cuts * ctx["months"], 10
    if strategy == "redirect_retirement":
        return ctx["ira_monthly"] * ctx["months"], 7.5
    if strategy == "use_emergency_fund":
        return max(0.0, ctx["emergency_balance"] - ctx["emergency_min"]), 6
    if strategy == "cuts_and_emergency_fund":
        return cuts * ctx["months"] + max(0.0, ctx["emergency_balance"] - ctx["emergency_min"]), 6
    return (cuts + ctx["ira_monthly"]) * ctx["months"], 7.5


def lever_text(lv: dict) -> str:
    t = lv["type"]
    if t == "cancel_subscription":
        return f"cancel {lv['target']}"
    if t == "cut_category":
        return f"reduce {lv['target']} by ${lv['amount']:,.2f}/month"
    if t == "reduce_ira":
        return f"reduce IRA contribution by ${lv['amount']:,.2f}/month"
    if t == "pause_ira":
        return "pause IRA contributions"
    if t == "use_emergency_fund":
        return f"move ${lv['amount']:,.0f} from the emergency fund"
    return t


# ---- LLM roles -----------------------------------------------------------------

def _ctx_text(ctx: dict) -> str:
    return json.dumps({
        "goal": f"${ctx['target']:,.0f} by {ctx['deadline']}", "current_balance": ctx["goal_balance"],
        "monthly_transfer": ctx["goal_transfer"], "forecast": ctx["forecast"], "gap": ctx["gap"],
        "months_left": ctx["months"], "needed_per_month": ctx["monthly_needed"],
        "subscriptions": ctx["subscriptions"], "keep": ctx["keep"], "category_avg_monthly": ctx["category_avg"],
        "max_category_cut": "25%", "ira_monthly": ctx["ira_monthly"],
        "emergency_fund": ctx["emergency_balance"], "emergency_min": ctx["emergency_min"]})


def lever_menu(ctx: dict, allowed: list[str]) -> str:
    """Spell out every legal lever and its limit so a small model does not have to derive the bounds."""
    cap = policy()["bounds"]["max_category_cut"]
    keep = {k.lower() for k in ctx["keep"]}
    lines = []
    if "cancel_subscription" in allowed:
        subs = [f"{s['merchant']} (${s['price']:.2f})" for s in ctx["subscriptions"] if s["merchant"].lower() not in keep]
        lines.append(f"- cancel_subscription: target one of {', '.join(subs)}; amount null. NEVER cancel: {', '.join(ctx['keep']) or 'none'}.")
    if "cut_category" in allowed:
        cats = [f"{c} up to ${ctx['category_avg'][c] * cap:.2f}" for c in policy()["bounds"]["discretionary"] if c in ctx["category_avg"]]
        lines.append(f"- cut_category: target and monthly amount, at most: {', '.join(cats)}.")
    if "reduce_ira" in allowed:
        lines.append(f"- reduce_ira: amount up to ${ctx['ira_monthly']:.2f}/month (target null).")
    if "pause_ira" in allowed:
        lines.append(f"- pause_ira: stops the ${ctx['ira_monthly']:.2f}/month IRA contribution (target and amount null). Do not combine with reduce_ira.")
    if "use_emergency_fund" in allowed:
        lines.append(f"- use_emergency_fund: one-time amount up to ${max(0, ctx['emergency_balance'] - ctx['emergency_min']):,.2f} (target null).")
    return "\n".join(lines)


GEN_STRATEGY_SCHEMA = {"type": "object", "properties": {"thoughts": {"type": "array", "items": {
    "type": "object", "properties": {"strategy": {"type": "string", "enum": list(STRATEGIES)}, "rationale": {"type": "string"}},
    "required": ["strategy", "rationale"]}}}, "required": ["thoughts"]}

GEN_LEVERS_SCHEMA = {"type": "object", "properties": {"thoughts": {"type": "array", "items": {
    "type": "object", "properties": {
        "levers": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": LEVER_TYPES}, "target": {"type": ["string", "null"]},
            "amount": {"type": ["number", "null"]}}, "required": ["type", "target", "amount"]}},
        "rationale": {"type": "string"}}, "required": ["levers", "rationale"]}}}, "required": ["thoughts"]}

GEN_SCHEDULE_SCHEMA = {"type": "object", "properties": {"thoughts": {"type": "array", "items": {
    "type": "object", "properties": {"start_month": {"type": "string"}, "rationale": {"type": "string"}},
    "required": ["start_month", "rationale"]}}}, "required": ["thoughts"]}

CRITIC_SCHEMA = {"type": "object", "properties": {"scores": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "sustainability": {"type": "integer", "minimum": 1, "maximum": 10},
                                     "evidence": {"type": "string"}}, "required": ["id", "sustainability", "evidence"]}}},
    "required": ["scores"]}

CRITIC_RUBRIC = ("Score each candidate's SUSTAINABILITY from 1 to 10: how likely a person keeps the plan for months. "
                 "10 = one or two small, painless changes; 7 = a few moderate changes; 4 = many changes or large cuts; "
                 "1 = unrealistic. Penalize plans that cut things the user values. Ignore goal coverage and safety: code scores those. "
                 "For each score, cite one tool fact (monthly_added, changes, coverage) in 'evidence'.")


class Planner:
    def __init__(self, ctx: dict, trace_id: str, use_llm: bool = True):
        self.ctx, self.trace_id, self.use_llm = ctx, trace_id, use_llm
        self.budget = policy()["budget"]
        self.started = time.time()
        self.calls = 0
        self.nodes: list[dict] = []
        self.events: list[str] = []

    # budget helpers
    def _can_call(self, reserve: int = 2) -> bool:
        in_time = time.time() - self.started < self.budget["tot_minutes"] * 60
        return self.use_llm and in_time and self.calls < self.budget["tot_llm_calls"] - reserve

    def _call(self, messages, schema, role, temperature):
        self.calls += 1
        return llm.chat_json(messages, role=role, schema=schema, temperature=temperature, seed=7 + self.calls)

    def _node(self, depth, parent, thought, **kw) -> dict:
        node = {"id": f"N{len(self.nodes)}", "depth": depth, "parent": parent, "thought": thought, "status": "open",
                "prune_reason": None, "S": None, "V": None, **kw}
        self.nodes.append(node)
        return node

    # generator
    def _generate(self, depth: int, parent: dict | None, k: int, feedback: str = "", reserve: int = 2) -> list[dict]:
        ctx_text = _ctx_text(self.ctx)
        if depth == 1:
            schema, ask = GEN_STRATEGY_SCHEMA, (f"Propose {k} DIFFERENT high-level strategies to close the gap. Choose from: "
                                                f"{', '.join(STRATEGIES)}.")
        elif depth == 2:
            allowed = STRATEGIES[parent["thought"]["strategy"]]["levers"]
            schema, ask = GEN_LEVERS_SCHEMA, (
                f"Strategy: {parent['thought']['strategy']}. Propose {k} DIFFERENT concrete plans. Allowed levers and limits:\n"
                f"{lever_menu(self.ctx, allowed)}\n"
                f"Each plan's monthly savings must add up to at least ${self.ctx['monthly_needed']:.2f}. "
                "Prefer few changes. Use only the levers listed above and stay within every limit.")
        else:
            schema, ask = GEN_SCHEDULE_SCHEMA, (
                f"Plan: {'; '.join(lever_text(l) for l in parent['thought']['levers'])}. "
                f"Propose {k} DIFFERENT start months (YYYY-MM) between {self.ctx['start_month']} and {self.ctx['deadline']} for this plan, "
                "for example starting immediately versus after a known busy period.")
        if feedback:
            ask += f"\nYour previous proposals were rejected by code checks: {feedback}. Fix these problems."
        if self._can_call(reserve):
            try:
                out = self._call([
                    {"role": "system", "content": "You are MoneyRoute's plan generator. Reply only with JSON. Use only facts given."},
                    {"role": "user", "content": f"Facts: {ctx_text}\n\n{ask}"}], schema, "planner_generator",
                    policy()["models"]["generator_temperature"])
                thoughts = out.get("thoughts", [])[:k]
                if thoughts:
                    return thoughts
            except llm.LLMError as exc:
                self.events.append(f"generator error at depth {depth}: {exc}; using code fallback")
        self.events.append(f"depth {depth}: code fallback generator used")
        return self._fallback(depth, parent, k)

    def _fallback(self, depth: int, parent: dict | None, k: int) -> list[dict]:
        """Deterministic candidates so the planner still returns a plan when the LLM fails or the budget runs out."""
        if depth == 1:
            return [{"strategy": s, "rationale": "fallback"} for s in list(STRATEGIES)[:k]]
        if depth == 3:
            return [{"start_month": self.ctx["start_month"], "rationale": "start now"},
                    {"start_month": add_months(self.ctx["start_month"], 2), "rationale": "start later"}][:k]
        need = self.ctx["monthly_needed"]
        cap = policy()["bounds"]["max_category_cut"]
        strategy = parent["thought"]["strategy"]
        allowed = STRATEGIES[strategy]["levers"]
        subs = sorted([s for s in self.ctx["subscriptions"] if s["merchant"].lower() not in {x.lower() for x in self.ctx["keep"]}],
                      key=lambda s: -s["price"])
        cats = sorted([c for c in policy()["bounds"]["discretionary"] if c in self.ctx["category_avg"]],
                      key=lambda c: -self.ctx["category_avg"][c])
        ef_room = max(0.0, self.ctx["emergency_balance"] - self.ctx["emergency_min"])

        def build(n_subs: int) -> dict:
            levers, total = [], 0.0
            if "use_emergency_fund" in allowed and "cut_category" not in allowed:
                return {"levers": [{"type": "use_emergency_fund", "target": None, "amount": round(ef_room, 2)}], "rationale": "fallback"}
            if "cancel_subscription" in allowed:
                for s in subs[:n_subs]:
                    levers.append({"type": "cancel_subscription", "target": s["merchant"], "amount": None}); total += s["price"]
            if "cut_category" in allowed:
                for c in cats:
                    if total >= need:
                        break
                    amt = round(min(self.ctx["category_avg"][c] * cap, need - total + 0.01), 2)
                    levers.append({"type": "cut_category", "target": c, "amount": amt}); total += amt
            if total < need and "reduce_ira" in allowed and self.ctx["ira_monthly"] > 0:
                amt = round(min(self.ctx["ira_monthly"], need - total + 0.01), 2)
                levers.append({"type": "reduce_ira", "target": None, "amount": amt}); total += amt
            if total < need and "use_emergency_fund" in allowed and ef_room > 0:
                lump = round(min(ef_room, (need - total) * self.ctx["months"] + 1), 2)
                levers.append({"type": "use_emergency_fund", "target": None, "amount": lump})
            return {"levers": levers, "rationale": "fallback"}

        def fewest() -> dict:
            """Greedy: safest, largest levers first, so the plan needs as few changes as possible."""
            opts = []
            if "cut_category" in allowed:
                opts += [(0, self.ctx["category_avg"][c] * cap, {"type": "cut_category", "target": c}) for c in cats]
            if "cancel_subscription" in allowed:
                opts += [(0, s["price"], {"type": "cancel_subscription", "target": s["merchant"], "amount": None}) for s in subs]
            if "reduce_ira" in allowed and self.ctx["ira_monthly"] > 0:
                opts.append((2.5, self.ctx["ira_monthly"] - 0.01, {"type": "reduce_ira", "target": None}))
            if "use_emergency_fund" in allowed and ef_room > 0:
                opts.append((4, ef_room / self.ctx["months"], {"type": "use_emergency_fund", "target": None}))
            opts.sort(key=lambda o: (o[0], -o[1]))
            levers, total = [], 0.0
            for _, capacity, lv in opts:
                if total >= need:
                    break
                take = min(capacity, need - total + 0.01)
                lv = dict(lv)
                if lv["type"] == "use_emergency_fund":
                    lv["amount"] = round(min(ef_room, take * self.ctx["months"] + 1), 2)
                elif lv["type"] != "cancel_subscription":
                    lv["amount"] = round(take, 2)
                else:
                    take = capacity
                levers.append(lv); total += take
            return {"levers": levers, "rationale": "fallback: fewest changes"}

        if strategy == "redirect_retirement":
            plans = [{"levers": [{"type": "reduce_ira", "target": None, "amount": round(min(self.ctx["ira_monthly"], need + 0.01), 2)}], "rationale": "fallback"},
                     {"levers": [{"type": "pause_ira", "target": None, "amount": None}], "rationale": "fallback"}]
        else:
            plans, seen = [], set()
            for p in [fewest()] + [build(n) for n in (1, len(subs), 0)]:
                key = json.dumps(p["levers"], sort_keys=True)
                if key not in seen:
                    seen.add(key); plans.append(p)
        return plans[:k]

    # critic
    def _critique(self, siblings: list[dict], shuffle: bool = False) -> None:
        live = [n for n in siblings if n["status"] == "open"]
        if not live:
            return
        if not self._can_call(reserve=0 if shuffle else 2):
            for n in live:
                n["S"] = n["S"] or 5
                n["critic_evidence"] = "budget exhausted: neutral score"
            return
        order = live[:]
        if shuffle:
            random.Random(len(self.nodes)).shuffle(order)
        cands = [{"id": n["id"], "plan": n["summary"], "monthly_added": n["eval"]["monthly_added"], "changes": n["eval"]["changes"],
                  "coverage_of_gap": f"{min(100, 100 * n['eval']['coverage'] / max(self.ctx['gap'], 1)):.0f}%"} for n in order]
        try:
            out = self._call([
                {"role": "system", "content": "You are MoneyRoute's plan critic. " + CRITIC_RUBRIC},
                {"role": "user", "content": f"User facts: {_ctx_text(self.ctx)}\nCandidates: {json.dumps(cands)}"}],
                CRITIC_SCHEMA, "planner_critic", 0.0)
            scores = {s["id"]: s for s in out.get("scores", [])}
        except llm.LLMError as exc:
            self.events.append(f"critic error: {exc}")
            scores = {}
        for n in live:
            s = scores.get(n["id"])
            n["S"] = max(1, min(10, int(s["sustainability"]))) if s else 5
            n["critic_evidence"] = s["evidence"] if s else "no critic score: neutral 5"

    def _score(self, nodes):
        for n in nodes:
            if n["status"] == "open":
                n["V"] = round(0.5 * n["eval"]["G"] + 0.3 * n["S"] + 0.2 * n["eval"]["R"], 2)

    def _select(self, nodes: list[dict]) -> list[dict]:
        width, floor = self.budget["beam_width"], self.budget["prune_below"]
        for n in nodes:
            if n["status"] == "open" and n["V"] < floor:
                n["status"], n["prune_reason"] = "pruned", f"score {n['V']} below {floor}"
        live = sorted([n for n in nodes if n["status"] == "open"], key=lambda n: (-n["V"], -n["eval"]["R"], n["eval"]["changes"]))
        if len(live) > width and abs(live[width - 1]["V"] - live[width]["V"]) <= 0.5:
            tied = [live[width - 1], live[width]]
            self.events.append(f"tie between {tied[0]['id']} and {tied[1]['id']}: re-scoring in shuffled order")
            self._critique(tied, shuffle=True)
            self._score(tied)
            live = sorted(live, key=lambda n: (-n["V"], -n["eval"]["R"], n["eval"]["changes"]))
        for n in live[width:]:
            n["status"], n["prune_reason"] = "pruned", "outside beam"
        for n in live[:width]:
            n["status"] = "kept"
        return live[:width]

    # search
    def run(self) -> dict:
        ctx = self.ctx
        root = self._node(0, None, {"gap": ctx["gap"], "needed_per_month": ctx["monthly_needed"]}, summary=f"Gap ${ctx['gap']:,.2f} (+${ctx['monthly_needed']:,.2f}/month)", status="kept")
        # depth 1: strategies
        d1 = []
        for t in self._generate(1, root, 3):
            if t["strategy"] not in STRATEGIES or any(n["thought"]["strategy"] == t["strategy"] for n in d1):
                continue
            capacity, r = strategy_capacity(ctx, t["strategy"])
            node = self._node(1, root["id"], t, summary=STRATEGIES[t["strategy"]]["label"],
                              eval={"G": round(10 * min(1, capacity / ctx["gap"]), 2), "R": r, "monthly_added": round(capacity / ctx["months"], 2),
                                    "coverage": round(capacity, 2), "changes": 1, "violations": []})
            if capacity < ctx["gap"]:
                node["status"], node["prune_reason"] = "impossible", f"can add at most ${capacity:,.0f}, gap is ${ctx['gap']:,.0f}"
                if t["strategy"] == "use_emergency_fund":
                    node["prune_reason"] += f"; covering the gap would leave ${ctx['emergency_balance'] - ctx['gap']:,.0f} in the emergency fund"
            d1.append(node)
        self._critique(d1); self._score(d1)
        beam = self._select(d1)
        # depth 2: concrete levers
        d2_all = []
        for parent in beam:
            allowed = STRATEGIES[parent["thought"]["strategy"]]["levers"]

            def expand(thoughts, source):
                kids = []
                for t in thoughts:
                    levers = [l for l in t.get("levers", []) if l.get("type") in LEVER_TYPES]
                    ev = evaluate_levers(ctx, levers)
                    off_strategy = [l["type"] for l in levers if l["type"] not in allowed]
                    node = self._node(2, parent["id"], {"levers": levers, "rationale": t.get("rationale", "")},
                                      summary="; ".join(lever_text(l) for l in levers) or "(empty plan)", eval=ev, source=source)
                    if not levers:
                        node["status"], node["prune_reason"] = "impossible", "empty plan"
                    elif ev["violations"] or off_strategy:
                        node["status"], node["prune_reason"] = "impossible", "; ".join(ev["violations"] + [f"lever {o} not in strategy" for o in off_strategy])
                    kids.append(node)
                return kids

            def viable(nodes):
                return [n for n in nodes if n["status"] == "open" and n["eval"]["coverage"] + 0.01 >= ctx["gap"]]

            def problems(nodes):
                return " | ".join(n["prune_reason"] or f"'{n['summary']}' adds only ${n['eval']['monthly_added']:,.2f}/month, "
                                  f"needs ${ctx['monthly_needed']:,.2f}" for n in nodes)

            kids = expand(self._generate(2, parent, 3), "llm")
            if not viable(kids):
                self.events.append(f"{parent['id']}: no child passes the checks and covers the gap; retrying with feedback")
                kids += expand(self._generate(2, parent, 3, feedback=problems(kids), reserve=0), "llm-retry")
            if not viable(kids):
                self.events.append(f"{parent['id']}: code fallback candidates added")
                kids += expand(self._fallback(2, parent, 3), "code-fallback")
            self._critique(kids); self._score(kids)
            d2_all.extend(kids)
        beam = self._select(d2_all)
        # depth 3: schedule
        d3_all = []
        for parent in beam:
            kids = []
            for t in self._generate(3, parent, 2):
                start = str(t.get("start_month", ""))[:7]
                ev = dict(parent["eval"])
                node = self._node(3, parent["id"], {"levers": parent["thought"]["levers"], "start_month": start, "rationale": t.get("rationale", "")},
                                  summary=f"{parent['summary']}; start {start}", eval=ev)
                if not (len(start) == 7 and ctx["start_month"] <= start <= ctx["deadline"]):
                    node["status"], node["prune_reason"] = "impossible", f"start month {start!r} outside {ctx['start_month']}..{ctx['deadline']}"
                else:
                    fc = forecast_goal(ctx, ev["monthly_added"], ev["lump"], start)
                    ev["forecast"] = fc
                    covered = fc - ctx["forecast"]
                    ev["G"] = round(10 * min(1, covered / ctx["gap"]), 2)
                    if fc < ctx["target"]:
                        node["status"], node["prune_reason"] = "impossible", f"reaches ${fc:,.0f}, short of ${ctx['target']:,.0f}"
                kids.append(node)
            self._critique(kids); self._score(kids)
            d3_all.extend(kids)
        leaves = sorted([n for n in d3_all if n["status"] == "open"], key=lambda n: (-n["V"], -n["eval"]["R"], n["eval"]["changes"]))
        for n in leaves:
            n["status"] = "kept"
        best = leaves[0] if leaves else None

        def levers_key(n):
            return json.dumps(sorted((l["type"], (l.get("target") or "").lower()) for l in n["thought"]["levers"]))

        others = [n for n in leaves[1:] if levers_key(n) != levers_key(best)] if best else []
        runner = next((n for n in others if n["parent"] != best["parent"]), others[0] if others else None) if best else None
        if best:
            best["status"] = "chosen"
        if runner:
            runner["status"] = "runner_up"
        result = {"status": "plan" if best else "no_feasible_plan", "best": best, "runner_up": runner,
                  "nodes": self.nodes, "llm_calls": self.calls, "seconds": round(time.time() - self.started, 1), "events": self.events}
        if not best:
            closest = max((n for n in self.nodes if n["depth"] >= 2 and not n["eval"].get("violations")), key=lambda n: n["eval"]["coverage"], default=None)
            result["closest"] = closest
            result["shortfall"] = round(ctx["gap"] - (closest["eval"]["coverage"] if closest else 0), 2)
        self._persist(result)
        return result

    def _persist(self, result):
        """plan-tree store: every node, its parent, scores and prune reason, for audit and replay."""
        db = sqlite3.connect(runtime_dir("plan_tree") / "plan_tree.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS nodes (trace TEXT, id TEXT, depth INT, parent TEXT, summary TEXT, status TEXT, "
                   "prune_reason TEXT, G REAL, S REAL, R REAL, V REAL, thought TEXT)")
        for n in self.nodes:
            ev = n.get("eval") or {}
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                self.trace_id, n["id"], n["depth"], n["parent"], n["summary"], n["status"], n["prune_reason"],
                ev.get("G"), n["S"], ev.get("R"), n["V"], json.dumps(n["thought"])))
        db.commit()
        db.close()


def oracle(ctx: dict) -> dict:
    """Exhaustive check used only in evaluation: best achievable safety score and fewest levers at that score."""
    cap = policy()["bounds"]["max_category_cut"]
    keep = {k.lower() for k in ctx["keep"]}
    options = [("cancel", s["merchant"], s["price"], 0) for s in ctx["subscriptions"] if s["merchant"].lower() not in keep]
    options += [("cut", c, ctx["category_avg"][c] * cap, 0) for c in policy()["bounds"]["discretionary"] if c in ctx["category_avg"]]
    if ctx["ira_monthly"]:
        options += [("reduce_ira", "IRA", ctx["ira_monthly"] - 0.01, 2.5), ("pause_ira", "IRA", ctx["ira_monthly"], 5)]
    ef = max(0.0, ctx["emergency_balance"] - ctx["emergency_min"])
    best = None
    for r in range(1, min(6, len(options)) + 1):
        for combo in itertools.combinations(options, r):
            if sum(1 for o in combo if o[0] in ("reduce_ira", "pause_ira")) > 1:
                continue
            for use_ef in (False, True):
                if use_ef and ef <= 0:
                    continue
                added = sum(o[2] for o in combo) * ctx["months"] + (ef if use_ef else 0)
                if added + 0.005 < ctx["gap"]:
                    continue
                safety = 10 - sum(o[3] for o in combo) - (4 if use_ef else 0)
                levers = r + (1 if use_ef else 0)
                key = (-safety, levers)
                if best is None or key < best["key"]:
                    best = {"key": key, "R": safety, "levers": levers, "combo": [o[1] for o in combo] + (["emergency fund"] if use_ef else [])}
    if not best:
        return {"feasible": False}
    return {"feasible": True, "R": best["R"], "levers": best["levers"], "example": best["combo"]}

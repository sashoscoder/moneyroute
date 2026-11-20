"""MoneyRoute as a LangGraph StateGraph (Checkpoint 5.1 coordination + Checkpoint 6.1 gates).

sync (G1) -> supervisor_plan (G2) -> Send() fan-out: ledger_analyst || guidance_researcher
-> join (calculator) -> goal_planner (only if off track) -> supervisor_draft -> output_gate (G4 + reviewer)
-> revise (<= 2) -> risk_router -> approval (interrupt) -> commit (memory) -> END
"""

import operator
import time
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from moneyroute import agents, audit, memory
from moneyroute.config import policy, runtime_dir
from moneyroute.gateway import build_ledger
from moneyroute.ledger import Ledger
from moneyroute.llm import COUNTER
from moneyroute.synth import answer_key, generate_feed, load_profile


class CheckInState(TypedDict, total=False):
    trace_id: str
    request: str
    profile_id: str
    month: str
    ledger_path: str
    gate_g1: dict
    plan: dict
    findings: Annotated[list[dict], operator.add]
    goal_plan: dict
    sheet: list[str]
    draft: str
    draft_source: str
    checks: list[dict]
    revisions: int
    items: list[dict]
    decisions: dict
    memory_written: list[dict]
    timings: Annotated[list[dict], operator.add]


def _timed(name):
    def deco(fn):
        def wrapper(state, *a, **kw):
            t = time.time()
            out = fn(state, *a, **kw) or {}
            out.setdefault("timings", []).append({"node": name, "seconds": round(time.time() - t, 2)})
            return out
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


@_timed("supervisor_plan")
def n_supervisor_plan(state: CheckInState):
    plan = agents.supervisor_plan(state["request"], state["month"])
    audit.log(state["trace_id"], "supervisor", "G2_plan", plan=plan)
    return {"plan": plan, "revisions": 0}


def fan_out(state: CheckInState):
    plan = state["plan"]
    if plan["intent"] == "out_of_scope":
        return "blocked"
    sends = []
    if {"ledger_review", "ledger_question"} & set(plan["tasks"]):
        sends.append(Send("ledger_analyst", {"plan": plan, "ledger_path": state["ledger_path"], "month": state["month"],
                                             "profile_id": state["profile_id"], "trace_id": state["trace_id"]}))
    if "guidance" in plan["tasks"] and plan.get("guidance_task"):
        # Only the task text crosses this edge: the guidance researcher never sees ledger data.
        task = plan["guidance_task"] if plan["intent"] == "monthly_checkin" else f"{plan['guidance_task']}. User question: {state['request']}"
        sends.append(Send("guidance_researcher", {"plan": {"guidance_task": task, "tax_year": plan["tax_year"]},
                                                  "profile_id": state["profile_id"], "trace_id": state["trace_id"]}))
    return sends or "join"


@_timed("ledger_analyst")
def n_ledger_analyst(state: dict):
    ledger = Ledger(state["ledger_path"])
    plan = state["plan"]
    found = []
    if "ledger_review" in plan["tasks"]:
        found += agents.ledger_review(ledger, state["month"], state["profile_id"])
    if "ledger_question" in plan["tasks"] and plan.get("ledger_question"):
        found += agents.ledger_question(ledger, plan["ledger_question"], plan["tax_year"])
    audit.log(state["trace_id"], "ledger_analyst", "G3_tools", queries=ledger.queries, findings=[f["id"] for f in found])
    return {"findings": found}


@_timed("guidance_researcher")
def n_guidance_researcher(state: dict):
    age = memory.facts(state["profile_id"])["age_band"]
    found = agents.guidance_research(state["plan"]["guidance_task"], state["plan"]["tax_year"], age)
    audit.log(state["trace_id"], "guidance_researcher", "retrieval", status=found[0]["kind"], data=found[0]["data"].get("checks"))
    return {"findings": found}


def n_blocked(state: CheckInState):
    kinds = ", ".join(state["plan"]["blocked"]) or "out of scope"
    f = agents.finding("B1", "supervisor", "out_of_scope",
                       f"This request is outside what MoneyRoute does ({kinds}). It does not pick investments, take tax-filing positions, "
                       "or move money. Please consult a licensed financial or tax professional, or act directly with your bank.",
                       confidence=1.0, signals={"policy": "money_movement" if "money_movement" in kinds else "out_of_scope"})
    audit.log(state["trace_id"], "supervisor", "G2_block", reasons=state["plan"]["blocked"])
    return {"findings": [f]}


@_timed("join")
def n_join(state: CheckInState):
    return {"findings": agents.calculator(state.get("findings", []))}


def after_join(state: CheckInState):
    if policy()["mode"] == "summary_only":
        return "supervisor_draft"
    if "goal_check" in state["plan"]["tasks"] and agents.planner_context(state["findings"], state["profile_id"]):
        return "goal_planner"
    return "supervisor_draft"


@_timed("goal_planner")
def n_goal_planner(state: CheckInState):
    ctx = agents.planner_context(state["findings"], state["profile_id"])
    gp = agents.goal_plan(ctx, state["trace_id"])
    audit.log(state["trace_id"], "goal_planner", "G2_tot", status=gp["status"], llm_calls=gp["llm_calls"],
              best=gp["best"] and gp["best"]["text"], pruned=[(n["id"], n["prune_reason"]) for n in gp["nodes"] if n["prune_reason"]])
    return {"goal_plan": gp}


@_timed("supervisor_draft")
def n_supervisor_draft(state: CheckInState):
    sheet = agents.facts_sheet(state["findings"], state.get("goal_plan"))
    feedback = None
    if state.get("checks"):
        last = state["checks"][-1]
        problems = [f"unsupported figure ${x:,.2f}" for x in last["code"]["unsupported_figures"]]
        problems += [f"year {y} is not in the facts" for y in last["code"].get("unsupported_years", [])]
        problems += [f"percentage {p}% is not in the facts" for p in last["code"].get("unsupported_percentages", [])]
        problems += [f"unknown citation {c}" for c in last["code"]["unknown_citations"]]
        problems += [f"{i['claim']}: {i['problem']}" for i in last.get("review", {}).get("issues", [])]
        feedback = "; ".join(problems)
    blocked = next((f for f in state["findings"] if f["kind"] == "out_of_scope"), None)
    if blocked:
        return {"sheet": sheet, "draft": blocked["claim"], "draft_source": "policy"}
    draft = agents.supervisor_draft(state["request"], sheet, feedback)
    return {"sheet": sheet, "draft": draft, "draft_source": "llm"}


@_timed("output_gate")
def n_output_gate(state: CheckInState):
    code = agents.check_figures(state["draft"], state["sheet"])
    rev = {"approved": True, "issues": []}
    if code["passed"] and state["draft_source"] == "llm":
        rev = agents.review(state["draft"], state["sheet"], memory.facts(state["profile_id"]).get("keep", []))
    check = {"revision": state.get("revisions", 0), "code": code, "review": rev, "passed": code["passed"] and rev["approved"]}
    audit.log(state["trace_id"], "reviewer", "G4_output", **check)
    return {"checks": state.get("checks", []) + [check]}


def after_gate(state: CheckInState):
    last = state["checks"][-1]
    if last["passed"]:
        return "risk_router"
    if state.get("revisions", 0) < policy()["budget"]["revisions"]:
        return "revise"
    return "fallback"


def n_revise(state: CheckInState):
    return {"revisions": state.get("revisions", 0) + 1}


def n_fallback(state: CheckInState):
    audit.log(state["trace_id"], "supervisor", "G4_fallback", reason="draft failed review after max revisions")
    return {"draft": agents.template_draft(state["sheet"]), "draft_source": "template (LLM draft blocked)"}


@_timed("risk_router")
def n_risk_router(state: CheckInState):
    items = []
    for f in state["findings"]:
        if f["kind"] == "planner_inputs":
            continue
        base, actionable = "AUTO", None
        if f["kind"] in ("unmatched_transfer", "duplicate"):
            actionable = "question"
        items.append({"id": f["id"], "kind": f["kind"], "text": f["claim"], "confidence": f["confidence"], "signals": f["signals"],
                      "base_lane": base, "action": actionable, "data": f["data"]})
    gp = state.get("goal_plan")
    if gp and gp.get("best"):
        b = gp["best"]
        items.append({"id": "P1", "kind": "plan", "text": f"Adopt plan: {b['text']}, starting {b['start_month']} "
                      f"(goal transfer {agents.money(b['transfer'])}/month, forecast {agents.money(b['forecast'])}).",
                      "base_lane": "BATCH", "touches": b["touches"], "monthly_change": b["monthly_added"], "action": "approve",
                      "memory": "episodic", "summary": f"Adopted plan: {b['text']} from {b['start_month']}", "data": b})
        for lv in b["levers"]:
            if lv["type"] == "cancel_subscription":
                items.append({"id": f"D-{lv['target']}", "kind": "cancellation_draft", "text": f"Cancellation message for {lv['target']} (you send it).",
                              "base_lane": "BATCH", "action": "approve", "draft": agents.cancellation_message(lv["target"]), "data": lv})
        if gp.get("runner_up"):
            r = gp["runner_up"]
            items.append({"id": "P2", "kind": "plan_alternative", "text": f"Alternative: {r['text']}, starting {r['start_month']} "
                          f"(forecast {agents.money(r['forecast'])}).", "base_lane": "BATCH", "touches": r["touches"],
                          "monthly_change": r["monthly_added"], "action": "approve", "memory": "episodic",
                          "summary": f"Adopted plan: {r['text']} from {r['start_month']}", "data": r})
    elif gp and gp.get("status") == "no_feasible_plan":
        items.append({"id": "P0", "kind": "no_plan", "text": f"No plan within your limits closes the gap (short about {agents.money(gp['shortfall'])}). "
                      "Extend the deadline or lower the target?", "base_lane": "HUMAN_NOW", "action": "question", "why": ["goal infeasible"]})
    if state["draft_source"].startswith("template"):
        items.append({"id": "D0", "kind": "draft", "text": "The written summary failed review twice and was replaced by verified facts.",
                      "base_lane": "AUTO", "signals": {"policy": "grounding_failed"}})
    if state["plan"]["intent"] == "monthly_checkin" and policy()["mode"] != "summary_only":
        items.append({"id": "M1", "kind": "memory_update", "text": f"Save this check-in ({state['month']}) to memory.", "base_lane": "BATCH",
                      "action": "approve", "memory": "episodic", "summary": f"{state['month']} check-in reviewed"})
    items = agents.route(items)
    audit.log(state["trace_id"], "risk_router", "lanes", lanes={i["id"]: i["lane"] for i in items})
    return {"items": items}


def n_approval(state: CheckInState):
    pending = [i for i in state["items"] if i["lane"] in ("BATCH", "HUMAN_NOW") and i.get("action")]
    if not pending or policy()["mode"] == "summary_only":
        return {"decisions": {}}
    decisions = interrupt({"card": pending, "draft": state["draft"]})
    audit.log(state["trace_id"], "user", "approval", decisions=decisions)
    return {"decisions": decisions or {}}


def n_commit(state: CheckInState):
    approved = []
    for it in state["items"]:
        d = (state.get("decisions") or {}).get(it["id"])
        if d == "approve" and it.get("memory"):
            approved.append(it)
        elif isinstance(d, dict) and d.get("answer") and it["kind"] == "unmatched_transfer":
            approved.append({"memory": "procedural", "summary": f"{it['data']['merchant']} on {it['data']['date']}: {d['answer']}", "data": it["data"]})
        elif isinstance(d, dict) and d.get("edit") and it.get("memory"):
            approved.append({**it, "summary": d["edit"]})
    written = memory.commit(state["profile_id"], approved, state["month"]) if approved else []
    audit.log(state["trace_id"], "memory", "commit", written=len(written))
    return {"memory_written": written}


def build_graph(checkpointer=None):
    g = StateGraph(CheckInState)
    g.add_node("supervisor_plan", n_supervisor_plan)
    g.add_node("ledger_analyst", n_ledger_analyst)
    g.add_node("guidance_researcher", n_guidance_researcher)
    g.add_node("blocked", n_blocked)
    g.add_node("join", n_join)
    g.add_node("goal_planner", n_goal_planner)
    g.add_node("supervisor_draft", n_supervisor_draft)
    g.add_node("output_gate", n_output_gate)
    g.add_node("revise", n_revise)
    g.add_node("fallback", n_fallback)
    g.add_node("risk_router", n_risk_router)
    g.add_node("approval", n_approval)
    g.add_node("commit", n_commit)
    g.add_edge(START, "supervisor_plan")
    g.add_conditional_edges("supervisor_plan", fan_out, ["ledger_analyst", "guidance_researcher", "blocked", "join"])
    g.add_edge("ledger_analyst", "join")
    g.add_edge("guidance_researcher", "join")
    g.add_edge("blocked", "join")
    g.add_conditional_edges("join", after_join, ["goal_planner", "supervisor_draft"])
    g.add_edge("goal_planner", "supervisor_draft")
    g.add_edge("supervisor_draft", "output_gate")
    g.add_conditional_edges("output_gate", after_gate, {"risk_router": "risk_router", "revise": "revise", "fallback": "fallback"})
    g.add_edge("revise", "supervisor_draft")
    g.add_edge("fallback", "risk_router")
    g.add_edge("risk_router", "approval")
    g.add_edge("approval", "commit")
    g.add_edge("commit", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def prepare(profile_id: str = "alex", overrides: dict | None = None, reset_memory: bool = True, tag: str | None = None) -> dict:
    """Sync step: generate the synthetic feed, run it through G1, write the read-only ledger, seed memory."""
    spec = load_profile(profile_id, overrides)
    mem_id = tag or profile_id
    feed = generate_feed(spec)
    db = runtime_dir("ledgers") / f"{mem_id}.sqlite"
    g1 = build_ledger(feed, db)
    memory.seed(mem_id, spec, reset=reset_memory)
    return {"spec": spec, "ledger_path": str(db), "gate_g1": g1, "answer_key": answer_key(spec), "memory_id": mem_id}


def start(graph, request: str, prep: dict, thread_id: str | None = None) -> dict:
    trace = audit.new_trace_id()
    thread_id = thread_id or trace
    audit.log(trace, "gateway", "G1_input", **{k: v for k, v in prep["gate_g1"].items() if k != "injection_events"},
              injection_events=len(prep["gate_g1"]["injection_events"]))
    calls_before = len(COUNTER.calls)
    t = time.time()
    state = graph.invoke({"trace_id": trace, "request": request, "profile_id": prep["memory_id"], "month": prep["spec"]["checkin_month"],
                          "ledger_path": prep["ledger_path"], "gate_g1": prep["gate_g1"], "findings": [], "timings": []},
                         {"configurable": {"thread_id": thread_id}})
    return {"thread_id": thread_id, "trace_id": trace, "state": state, "seconds": round(time.time() - t, 1),
            "llm_calls": COUNTER.calls[calls_before:], "interrupted": "__interrupt__" in state}


def resume(graph, thread_id: str, decisions: dict) -> dict:
    return graph.invoke(Command(resume=decisions), {"configurable": {"thread_id": thread_id}})

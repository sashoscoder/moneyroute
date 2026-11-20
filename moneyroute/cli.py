"""Command-line entry point.

    python -m moneyroute.cli ingest
    python -m moneyroute.cli checkin [--profile alex] [--approve-all]
    python -m moneyroute.cli ask "How much more can I contribute to my IRA this year?"
"""

import argparse
import json
import sys

from moneyroute import graph as G
from moneyroute import guidance, llm


def show(run: dict) -> None:
    st = run["state"]
    print(f"\ntrace {run['trace_id']} | {run['seconds']} s | {len(run['llm_calls'])} LLM calls")
    print(f"plan: {json.dumps(st['plan'])}")
    print("\nFINDINGS")
    for f in st["findings"]:
        if f["kind"] != "planner_inputs":
            print(f"  [{f['id']}] ({f['agent']}) {f['claim']}")
    gp = st.get("goal_plan")
    if gp:
        print(f"\nTREE OF THOUGHT ({gp['llm_calls']} calls, {gp['seconds']} s)")
        for n in gp["nodes"]:
            print(f"  {'  ' * n['depth']}{n['id']} {n['status']:<10} V={n['V']} | {n['summary'][:90]}" + (f"  <- {n['prune_reason']}" if n['prune_reason'] else ""))
    print("\nOUTPUT GATE")
    for c in st["checks"]:
        print(f"  rev {c['revision']}: code={'pass' if c['code']['passed'] else c['code']} reviewer={'approved' if c['review']['approved'] else c['review']['issues']}")
    print(f"\nDRAFT ({st['draft_source']})\n{st['draft']}")
    print("\nRISK ROUTER")
    for i in st["items"]:
        print(f"  {i['lane']:<10} {i['id']:<14} {i['text'][:90]}" + (f"  ({'; '.join(i['why'])})" if i.get("why") else ""))


def main(argv=None):
    p = argparse.ArgumentParser(prog="moneyroute")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    c = sub.add_parser("checkin")
    c.add_argument("--profile", default="alex")
    c.add_argument("--approve-all", action="store_true")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--profile", default="alex")
    args = p.parse_args(argv)
    if not llm.available():
        sys.exit("Ollama is not reachable at the configured URL. Start it with `ollama serve`.")
    if args.cmd == "ingest":
        print(f"Indexed {guidance.ingest()} chunks")
        return
    graph = G.build_graph()
    prep = G.prepare(args.profile)
    request = "Run my monthly check-in." if args.cmd == "checkin" else args.question
    run = G.start(graph, request, prep)
    show(run)
    if run["interrupted"] and getattr(args, "approve_all", False):
        card = run["state"]["__interrupt__"][0].value["card"]
        final = G.resume(graph, run["thread_id"], {i["id"]: "approve" for i in card if i["lane"] == "BATCH" and i["kind"] != "plan_alternative"})
        print(f"\nMEMORY WRITTEN: {json.dumps(final.get('memory_written'), indent=1)}")


if __name__ == "__main__":
    main()

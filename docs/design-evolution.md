# Design evolution: checkpoints to implementation

## Across the program (Modules 1 to 6)

| Module | Checkpoint decision | Why it improved the system |
|---|---|---|
| 1 | Scoped MoneyRoute as **one local personal-assistant agent** for users with 10+ accounts, with a privacy gateway in front of the model and a rule that it never moves money | Fixed the boundary early: local models for privacy, read-only tools for safety, approval for every change |
| 2 | Refined plan → act → check into a **ReAct loop** with a step budget, short-term state and long-term memory (semantic, episodic, procedural) written only after confirmation | Observations, not model recall, drive each step; rejected advice (for example "cancel the gym") does not return |
| 3 | Added **retrieval only for public rules**, kept personal facts in SQL; found the **wrong-tax-year failure** and fixed it with year tags from title + heading, a year filter, verbatim-quote checks and abstention | Corrected a $1,000 error (2023 limit stated for 2026) and made every rule answer traceable |
| 4 | Limited **Tree of Thought to off-track goals**; beam search width 2, depth 3; code hard checks decide feasibility, an LLM critic scores only sustainability; budget of 12 calls | Avoids premature commitment and constraint violations without paying ToT cost on factual questions |
| 5 | Split into **five agents** only where tools, data, or independence differ; `Send()` fan-out, typed findings in shared state, a reviewer from another model family, at most two revisions | Guidance search never sees ledger data; the drafter does not grade its own work |
| 6 | Added **four gates (G1 to G4)**, a **max-signal risk router** with four lanes, a kill switch, redacted audit logs and a **40-case evaluation plan** | Failures become safe and visible; the evaluation turns claims into measurements |

## What changed while building (Module 7)

| Planned | Built | Reason |
|---|---|---|
| Plaid Sandbox feed | Deterministic synthetic feed generator (`synth.py`) with an **independent answer key** | Reproducible evaluation: every expected figure is computed from the profile, not from MoneyRoute's tools |
| CrewAI roles for the ToT generator and critic | Plain Python `Planner` class with two LLM roles | Same roles and temperatures with one fewer framework; LangGraph already owns control flow |
| Local MCP servers for ledger, forecaster, guidance, memory, plan-tree | In-process tools behind per-agent allowlists; plan-tree as SQLite | Simpler to run and test locally; MCP packaging is listed as a next step |
| OpenTelemetry trace store | Redacted JSONL audit log + plan-tree SQLite | Enough to replay a decision; no extra service |
| Reviewer returns schema-constrained JSON | Gemma 4 asked for JSON in the prompt and parsed leniently | Ollama 0.40 returns HTTP 501 for structured output with Gemma 4 |
| G4 checked dollar figures | G4 checks dollar figures, **years and percentages** | A 2027 abstention draft invented "limits are announced in 2024"; the $-only check missed it |
| Supervisor LLM decides scope | Out-of-scope needs a reason category (product pick, tax filing, money movement) **or** the code screen; otherwise code keeps the request in scope | The 8B supervisor blocked ordinary IRA questions as out of scope |
| Generator proposes levers from raw facts | Generator gets an explicit **lever menu with dollar caps**, one retry with code feedback, then a deterministic fallback | The first run proposed three plans that all broke the 25% category cap |
| Four strategies | Added **cuts + part of the emergency fund** | Evaluation case E02 had no strategy that could combine the two levers |
| Year filter on every guidance search | Year filter only for year-sensitive rules | CFPB emergency-fund guidance is undated and was filtered out |
| Runner-up = second-best leaf | Runner-up must use a different set of levers | The first runs offered the same plan twice with different start months |
| Citation = label the model returns | Code resolves the citation to the retrieved chunk that contains the quote verbatim; one retry with the failed check as feedback | The 8B model often put the quote into the citation field or paraphrased it, so correct answers abstained (first full evaluation: 5 of 14 questions) |
| Calculator trusts a verified amount | Plausibility check: an IRA limit below $1,000 or below contributions is not used | First full evaluation, case Q06: a verified quote about the $1,100 catch-up was read as the limit and reported $0 room |
| Scope screen matched "what … fund" | Product-pick pattern requires investment-fund terms; LLM-only scope flags are logged, not enforced | Case Q11, "What is my emergency fund balance?", was blocked |

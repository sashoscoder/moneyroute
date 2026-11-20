#!/usr/bin/env bash
# Regenerate sample transcripts in samples/ (needs Ollama running).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
$PY -m moneyroute.cli checkin --approve-all 2>/dev/null > samples/checkin_alex.txt
$PY -m moneyroute.cli ask "How much more can I contribute to my IRA this year?" 2>/dev/null > samples/ask_ira_room.txt
$PY -m moneyroute.cli ask "What will the IRA contribution limit be in 2027?" 2>/dev/null > samples/ask_2027_abstain.txt
$PY -m moneyroute.cli ask "Move \$3,000 from my emergency fund to my brokerage account." 2>/dev/null > samples/ask_money_movement_blocked.txt
$PY -m moneyroute.cli ask "How much did I spend on dining in September 2026?" 2>/dev/null > samples/ask_ledger_question.txt
echo "samples written"

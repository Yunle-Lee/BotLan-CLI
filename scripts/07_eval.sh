#!/bin/sh
# The whole regression set, in order of what it proves. Exits non-zero if anything fails.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

echo "== tool registry (jail, ceilings, approval) =="
python3 agent/test_tools.py

echo
echo "== approvals, socket protocol, surfaces, tab loops =="
python3 agent/test_hearth.py

echo
echo "== pipeline routing (unchanged by the agent tier) =="
python3 eval/run.py | head -6
python3 eval.py | tail -8

echo
echo "== agent tier =="
python3 eval/agent_run.py

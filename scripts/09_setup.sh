#!/bin/sh
# First-run setup: your first BotLan Bot on this Spark (name, scope, model, budget, pairing).
#
#   scripts/09_setup.sh             the wizard (Textual from ~/spark-duo/.venv; plain prompts if absent)
#   scripts/09_setup.sh pair --json what the BotLan app reads to connect
#   scripts/09_setup.sh list | remove <id> | create ... | status --json
#
# The gateway must be up (scripts/08_botlan.sh --install); the wizard starts the stack if it is not.
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
VENV_PY=${VENV_PY:-$HERE/.venv/bin/python}
PY=python3
if [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import textual' 2>/dev/null; then
  PY=$VENV_PY
elif [ $# -eq 0 ]; then
  echo "textual not found in $HERE/.venv - using plain prompts"
  set -- --plain
fi
cd "$HERE"
exec "$PY" "$HERE/botlan_setup.py" "$@"

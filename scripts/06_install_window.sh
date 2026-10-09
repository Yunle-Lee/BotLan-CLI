#!/bin/sh
# Install what the window needs (textual) into the project venv and put the launcher on PATH.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-$ROOT/.venv}
PY=${PY:-python3}

[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet textual
"$VENV/bin/python" -c 'import textual, sys; print("textual", textual.__version__, "on", sys.version.split()[0])'

BIN=${BIN_DEST:-$HOME/.local/bin}
if [ -d "$BIN" ]; then
  ln -sfn "$ROOT/bin/jevstep-window" "$BIN/jevstep-window"
  echo "on PATH: $BIN/jevstep-window -> $ROOT/bin/jevstep-window"
fi

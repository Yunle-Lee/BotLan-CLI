#!/bin/sh
# Start the whole system: stage 2 (llama-server, VLM) then the orchestrator.
# Both models end up resident in the GB10 unified memory pool at the same time.
set -eu
LC=${1:-$HOME/llama.cpp}
BIN=${BIN:-$LC/build-cuda/bin}
MODELS=${MODELS:-$HOME/spark-duo/models}
LOGS=${LOGS:-$HOME/spark-duo/logs}
VLM=$MODELS/gelab-zero-4b-Q4_K_M.gguf
MMPROJ=$MODELS/gelab-zero-4b-mmproj-f16.gguf
VPORT=${VPORT:-8080}
OPORT=${OPORT:-8090}
mkdir -p "$LOGS"

[ -f "$VLM" ]    || { echo "missing $VLM — run 03" >&2; exit 1; }
[ -f "$MMPROJ" ] || { echo "missing $MMPROJ — run 03" >&2; exit 1; }

if [ ! -f "$LOGS/vlm.pid" ] || ! kill -0 "$(cat "$LOGS/vlm.pid")" 2>/dev/null; then
  nohup "$BIN/llama-server" -m "$VLM" --mmproj "$MMPROJ" \
    -ngl 99 -c 32768 -fa on --host 127.0.0.1 --port "$VPORT" \
    > "$LOGS/vlm.log" 2>&1 &
  echo $! > "$LOGS/vlm.pid"
  echo "vlm: pid $(cat "$LOGS/vlm.pid") on :$VPORT"
fi

i=0
while [ $i -lt 90 ]; do
  curl -sf "http://127.0.0.1:$VPORT/health" >/dev/null 2>&1 && break
  sleep 2; i=$((i+1))
done

if [ ! -f "$LOGS/orchestrator.pid" ] || ! kill -0 "$(cat "$LOGS/orchestrator.pid")" 2>/dev/null; then
  JEV_SCORE_BIN=${JEV_SCORE_BIN:-$HOME/spark-duo/bin/jev-score} \
  nohup python3 "$HOME/spark-duo/orchestrator.py" --port "$OPORT" > "$LOGS/orchestrator.log" 2>&1 &
  echo $! > "$LOGS/orchestrator.pid"
  echo "orchestrator: pid $(cat "$LOGS/orchestrator.pid") on :$OPORT"
fi

sleep 3
curl -sf "http://127.0.0.1:$OPORT/health" && echo

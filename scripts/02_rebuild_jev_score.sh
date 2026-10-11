#!/bin/sh
# Relink jev-score against the CUDA-native llama.cpp build.
# Why: the shipped binary links ~/llama.cpp/build, which has GGML_CUDA=OFF, so Jev
# runs on the CPU even though the runtime asks jev-score for --ngl 999.
set -eu
LC=${1:-$HOME/llama.cpp}
CUDA_BUILD=${CUDA_BUILD:-$LC/build-cuda}
JEVDIR=${JEVDIR:-$HOME/models/Jev-Style-0.8B-Decision-v3-GGUF}
OUT=${OUT:-$HOME/spark-duo/bin/jev-score}
VIEW=${VIEW:-$HOME/spark-duo/lc-cuda}

[ -f "$CUDA_BUILD/bin/libllama.so" ] || { echo "no CUDA build at $CUDA_BUILD — run 01 first" >&2; exit 1; }

# Jev model files: ModelScope mirror first (reachable from a Spark that cannot reach Hugging Face; it also
# carries BotLan's prefix-reuse jev_style_decision_gguf.py), then Hugging Face.
if [ ! -f "$JEVDIR/build_jev_score.sh" ]; then
  mkdir -p "$JEVDIR"
  if command -v modelscope >/dev/null && modelscope download --model "${JEV_MS:-Karoli/Jev-Style-0.8B-Decision-v3-GGUF}" --local_dir "$JEVDIR"; then :
  elif command -v hf >/dev/null; then hf download chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF --local-dir "$JEVDIR"     --include "*Q4_K_M.gguf" "tokenizer/*" "*.json" "*.py" "*.cpp" "*.sh" "*.txt" LICENSE NOTICE
  else echo "no Jev model at $JEVDIR and neither modelscope nor hf is installed" >&2; exit 1; fi
fi

# build_jev_score.sh hardcodes $LC/build/bin, so hand it a view where build/bin IS the CUDA build.
mkdir -p "$VIEW/build"
ln -sfn "$LC/include" "$VIEW/include"
ln -sfn "$LC/ggml"    "$VIEW/ggml"
ln -sfn "$LC/vendor"  "$VIEW/vendor"
ln -sfn "$CUDA_BUILD/bin" "$VIEW/build/bin"

OUT="$OUT" sh "$JEVDIR/build_jev_score.sh" "$VIEW"

echo "--- linkage ---"
ldd "$OUT" | grep -Ei 'llama|ggml|cuda' || true

echo "--- gate smoke test (--ngl 999) ---"
python3 "$JEVDIR/jev_style_decision_gguf.py" --quant Q4_K_M --jev-score "$OUT" --ngl 999 \
  --state "The film was excellent." --question "What is the sentiment of this review?" \
  --options '["negative","positive"]' --category general_sentiment

#!/bin/sh
# Stage 2 model: stepfun-ai/GELab-Zero-4B-preview (StepFun's smallest model;
# a Qwen3-VL-4B fine-tune) -> GGUF for llama.cpp + a separate mmproj vision tower.
# Downloads the prebuilt GGUF (~3.3 GB) when it can; otherwise converts from the original weights.
set -eu
LC=${1:-$HOME/llama.cpp}
BIN=${BIN:-$LC/build-cuda/bin}
SRC=${SRC:-$HOME/spark-duo/models/GELab-Zero-4B-preview}
OUTDIR=${OUTDIR:-$HOME/spark-duo/models}
QUANT=${QUANT:-Q4_K_M}
# convert_hf_to_gguf.py needs torch + transformers + gguf; keep them out of the base env.
PY=${PY:-$HOME/spark-duo/.venv/bin/python}
F16=$OUTDIR/gelab-zero-4b-f16.gguf
FINAL=$OUTDIR/gelab-zero-4b-$QUANT.gguf
MMPROJ=$OUTDIR/gelab-zero-4b-mmproj-f16.gguf

# Fast path: the exact GGUF files this stack was benchmarked with, prebuilt on a DGX Spark
# (ModelScope, Apache-2.0, sha256 checked). Skips the 8.9 GB download, torch and the conversion.
# GELAB_FROM_SOURCE=1 forces the conversion below.
PREBUILT=${PREBUILT:-Karoli/GELab-Zero-4B-preview-GGUF-DGX-Spark}
if [ "${GELAB_FROM_SOURCE:-}" != 1 ] && [ "$QUANT" = Q4_K_M ] && command -v modelscope >/dev/null    && { [ -f "$FINAL" ] && [ -f "$MMPROJ" ] || modelscope download --model "$PREBUILT"           gelab-zero-4b-Q4_K_M.gguf gelab-zero-4b-mmproj-f16.gguf --local_dir "$OUTDIR"; }; then
  (cd "$OUTDIR" && printf '%s  %s
'     6b53cf5bc60b022056940ccc11fff666b7817123dece551faaa5c004f947527f gelab-zero-4b-Q4_K_M.gguf     7e2178f93462021c0b9f2b39f6abeafd747f8b35c318bd6f087972f33abb7274 gelab-zero-4b-mmproj-f16.gguf     | sha256sum -c -) && { ls -la "$FINAL" "$MMPROJ"; exit 0; }
  echo "prebuilt GGUF failed its checksum; converting from source instead" >&2
  rm -f "$FINAL" "$MMPROJ"
fi

if [ ! -x "$PY" ]; then
  python3 -m venv "$(dirname "$(dirname "$PY")")"
  "$(dirname "$PY")/pip" install -U pip torch transformers sentencepiece numpy >/dev/null
  "$(dirname "$PY")/pip" install "$LC/gguf-py" >/dev/null
fi
[ -x "$PY" ] || { echo "no python at $PY" >&2; exit 1; }

command -v modelscope >/dev/null || { echo "modelscope CLI missing: pip install modelscope" >&2; exit 1; }

if [ ! -f "$SRC/config.json" ]; then
  echo "downloading stepfun-ai/GELab-Zero-4B-preview (~8.9 GB) -> $SRC"
  modelscope download --model stepfun-ai/GELab-Zero-4B-preview --local_dir "$SRC"
fi

[ -f "$F16" ]    || "$PY" "$LC/convert_hf_to_gguf.py" "$SRC" --outtype f16 --outfile "$F16"
[ -f "$MMPROJ" ] || "$PY" "$LC/convert_hf_to_gguf.py" "$SRC" --mmproj --outtype f16 --outfile "$MMPROJ"
# convert_hf_to_gguf.py only emits f16/bf16/q8_0; K-quants need the quantizer.
[ -f "$FINAL" ]  || "$BIN/llama-quantize" "$F16" "$FINAL" "$QUANT"

ls -la "$OUTDIR"

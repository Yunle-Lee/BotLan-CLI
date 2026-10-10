#!/bin/sh
# Offline install on a DGX Spark from the BotLan USB stick: no compiling, no model downloads.
#
#   sh scripts/00_offline_install.sh /media/$USER/Ventoy/BotLan     (the BotLan folder on the stick)
#
# Stick layout (built on a DGX Spark, see README "Offline install from a USB stick"):
#   botlan-core.tar           llama.cpp CUDA build (sm_121, CUDA 13), jev-score, Python wheels.
#                             One tar because it holds Linux symlinks exFAT cannot store.
#   models/jev/               Jev-Style-0.8B-Decision-v3 Q4_K_M + tokenizer + readout config
#   models/gelab/             GELab-Zero-4B Q4_K_M + mmproj f16 (GGUF)
#   BotLan-CLI-src.tar        this repository
#   SHA256SUMS                checksums of everything above
# The binaries find their own libraries through $ORIGIN, so the account name and install path do
# not matter. They need an aarch64 machine with an NVIDIA driver and a CUDA 13 runtime
# (libcudart.so.13 / libcublas.so.13), which DGX OS ships; the script finds it wherever it lives.
set -eu
USB=${1:?usage: sh scripts/00_offline_install.sh <path-to>/BotLan}
HERE=$(cd "$(dirname "$0")/.." && pwd)
LCBIN=$HOME/llama.cpp/build-cuda/bin
step() { printf '\n[%s] %s\n' "$1" "$2"; }

step 1/6 "checking the stick and this machine"
[ -f "$USB/botlan-core.tar" ] || { echo "no botlan-core.tar under $USB (mount the stick: udisksctl mount -b /dev/sdX1)" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "the binaries are for aarch64 (DGX Spark); this is $(uname -m)" >&2; exit 1; }
command -v nvidia-smi >/dev/null && nvidia-smi -L || echo "warning: nvidia-smi not found - is the NVIDIA driver installed?"
# CUDA 13 runtime: honour CUDA_HOME, else any /usr/local/cuda*, else the system loader path.
CUDALIB=""
for d in ${CUDA_HOME:+$CUDA_HOME/targets/*/lib $CUDA_HOME/lib64} /usr/local/cuda/targets/*/lib /usr/local/cuda-13*/targets/*/lib /usr/local/cuda*/targets/*/lib /usr/local/cuda*/lib64; do
  [ -f "$d/libcudart.so.13" ] && [ -f "$d/libcublas.so.13" ] && { CUDALIB=$d; break; }
done
if [ -z "$CUDALIB" ] && ldconfig -p 2>/dev/null | grep -q 'libcudart.so.13'; then CUDALIB=system; fi
[ -n "$CUDALIB" ] || { echo "no CUDA 13 runtime (libcudart.so.13 + libcublas.so.13) found; set CUDA_HOME" >&2; exit 1; }
echo "CUDA runtime: $CUDALIB"
command -v python3 >/dev/null || { echo "python3 missing" >&2; exit 1; }
PYV=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
echo "python: $PYV"
echo "verifying checksums (about a minute)…"
(cd "$USB" && sha256sum -c --quiet SHA256SUMS) || { echo "the stick's files are damaged: checksum mismatch" >&2; exit 1; }
echo "stick OK"

step 2/6 "unpacking prebuilt binaries"
WORK=$HOME/.cache/botlan; mkdir -p "$WORK"
tar -C "$WORK" -xf "$USB/botlan-core.tar"
B=$WORK/botlan-offline
(cd "$B" && sha256sum -c --quiet SHA256SUMS) || { echo "unpacked bundle failed its checksums" >&2; exit 1; }
mkdir -p "$LCBIN" "$HERE/bin" "$HERE/lc-cuda/build"
cp -a "$B/llama-bin/." "$LCBIN/"
cp -a "$B/jev-score" "$HERE/bin/jev-score"; chmod +x "$HERE/bin/jev-score"
ln -sfn "$LCBIN" "$HERE/lc-cuda/build/bin"
# Only the CUDA runtime is outside the bundle; tell the loader where it is (read by 04_serve.sh).
if [ "$CUDALIB" = system ]; then : > "$HERE/.botlan-env"
else echo "export LD_LIBRARY_PATH=$CUDALIB\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}" > "$HERE/.botlan-env"; fi
. "$HERE/.botlan-env"
"$LCBIN/llama-server" --version 2>&1 | tail -2
if ldd "$LCBIN/llama-server" "$HERE/bin/jev-score" | grep -q 'not found'; then
  ldd "$LCBIN/llama-server" "$HERE/bin/jev-score" | grep 'not found' >&2; exit 1
fi

step 3/6 "installing models"
mkdir -p "$HOME/models/Jev-Style-0.8B-Decision-v3-GGUF" "$HERE/models"
cp -a "$USB/models/jev/." "$HOME/models/Jev-Style-0.8B-Decision-v3-GGUF/"
cp -a "$USB/models/gelab/gelab-zero-4b-Q4_K_M.gguf" "$USB/models/gelab/gelab-zero-4b-mmproj-f16.gguf" "$HERE/models/"

step 4/6 "installing Python packages from the stick (offline)"
W=$B/wheels
if ! python3 -c 'import tokenizers, numpy' 2>/dev/null; then
  python3 -m pip install --user --no-index --find-links "$W" tokenizers numpy 2>/dev/null \
    || python3 -m pip install --user --break-system-packages --no-index --find-links "$W" tokenizers numpy \
    || { echo "could not install tokenizers/numpy offline (wheels are for Python 3.12; this is $PYV)" >&2; exit 1; }
fi
if python3 -m venv "$HERE/.venv" 2>/dev/null; then
  "$HERE/.venv/bin/pip" install -q --no-index --find-links "$W" textual || echo "textual skipped (wizard uses plain prompts)"
fi

step 5/6 "starting both models and the BotLan gateway"
cd "$HERE"
sh scripts/04_serve.sh
# BOTLAN_NO_SYSTEMD=1: start the gateway without installing user units (side-by-side tests).
if [ "${BOTLAN_NO_SYSTEMD:-}" = 1 ]; then sh scripts/08_botlan.sh; else sh scripts/08_botlan.sh --install; fi

step 6/6 "done"
echo "next: sh scripts/09_setup.sh   (first Bot: name, scope, model, budget)"
echo "keep it running after you log out: sudo loginctl enable-linger $USER"
echo "then on the laptop: BotLan tray menu → 连接 DGX Spark…"

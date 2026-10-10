#!/bin/sh
# Offline install on a DGX Spark from the BotLan USB stick: no compiling, no model downloads.
#
#   sh scripts/00_offline_install.sh /media/$USER/Ventoy        (the stick's mount point)
#
# Stick layout (built on a DGX Spark, see README "Offline install from a USB stick"):
#   botlan-core.tar           llama.cpp CUDA build (sm_121, CUDA 13.0), jev-score, Python wheels.
#                             One tar because it holds Linux symlinks exFAT cannot store.
#   models/jev/               Jev-Style-0.8B-Decision-v3 Q4_K_M + tokenizer + readout config
#   models/gelab/             GELab-Zero-4B Q4_K_M + mmproj f16 (GGUF)
#   BotLan-CLI-src.tar        this repository
#   SHA256SUMS                checksums of everything above
# Needs the platform the binaries were built on: DGX OS (Ubuntu 24.04 aarch64), CUDA 13.0 at
# /usr/local/cuda-13.0. The binaries carry absolute RUNPATHs (/home/<user>/llama.cpp/build-cuda/bin),
# so files go to the standard paths; a different user name is handled with LD_LIBRARY_PATH.
set -eu
USB=${1:?usage: sh scripts/00_offline_install.sh <usb-mount-point>}
HERE=$(cd "$(dirname "$0")/.." && pwd)
CUDA=${CUDA_HOME:-/usr/local/cuda-13.0}
LCBIN=$HOME/llama.cpp/build-cuda/bin
step() { printf '\n[%s] %s\n' "$1" "$2"; }

step 1/6 "checking the stick and this machine"
[ -f "$USB/botlan-core.tar" ] || { echo "no botlan-core.tar under $USB" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "the binaries are for aarch64 (DGX Spark); this is $(uname -m)" >&2; exit 1; }
[ -f "$CUDA/targets/sbsa-linux/lib/libcudart.so.13" ] || { echo "CUDA 13.0 runtime not found under $CUDA" >&2; exit 1; }
command -v nvidia-smi >/dev/null && nvidia-smi -L || echo "warning: nvidia-smi not found"
echo "verifying checksums (about a minute)…"
(cd "$USB" && sha256sum -c --quiet SHA256SUMS) || { echo "the stick's files are damaged: checksum mismatch" >&2; exit 1; }
echo "stick OK"

step 2/6 "unpacking prebuilt binaries"
WORK=$HOME/.cache/botlan; mkdir -p "$WORK"
tar -C "$WORK" -xf "$USB/botlan-core.tar"
B=$WORK/botlan-offline
mkdir -p "$LCBIN"
cp -a "$B/llama-bin/." "$LCBIN/"
mkdir -p "$HERE/bin" "$HERE/lc-cuda/build"
cp -a "$B/jev-score" "$HERE/bin/jev-score"; chmod +x "$HERE/bin/jev-score"
ln -sfn "$LCBIN" "$HERE/lc-cuda/build/bin"
# Binaries were linked under /home/user1; on another account point the loader at the real paths.
if [ "$HOME" != /home/user1 ]; then
  echo "export LD_LIBRARY_PATH=$LCBIN:$CUDA/targets/sbsa-linux/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}" > "$HERE/.botlan-env"
  . "$HERE/.botlan-env"
  echo "note: built for /home/user1; using LD_LIBRARY_PATH (saved in $HERE/.botlan-env)"
fi
"$LCBIN/llama-server" --version 2>&1 | head -2

step 3/6 "installing models"
mkdir -p "$HOME/models/Jev-Style-0.8B-Decision-v3-GGUF" "$HERE/models"
cp -a "$USB/models/jev/." "$HOME/models/Jev-Style-0.8B-Decision-v3-GGUF/"
cp -a "$USB/models/gelab/gelab-zero-4b-Q4_K_M.gguf" "$USB/models/gelab/gelab-zero-4b-mmproj-f16.gguf" "$HERE/models/"

step 4/6 "installing Python packages from the stick (offline)"
W=$B/wheels
python3 -m pip install --user --no-index --find-links "$W" tokenizers numpy 2>/dev/null \
  || python3 -m pip install --user --break-system-packages --no-index --find-links "$W" tokenizers numpy
if python3 -m venv "$HERE/.venv" 2>/dev/null; then
  "$HERE/.venv/bin/pip" install -q --no-index --find-links "$W" textual || echo "textual skipped (wizard uses plain prompts)"
fi

step 5/6 "starting both models and the BotLan gateway"
cd "$HERE"
sh scripts/04_serve.sh
sh scripts/08_botlan.sh --install

step 6/6 "done"
echo "next: sh scripts/09_setup.sh   (first Bot: name, scope, model, budget)"
echo "then on the laptop: BotLan tray menu → 连接 DGX Spark…"

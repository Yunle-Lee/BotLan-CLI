#!/bin/sh
# BotLan one-command install for an NVIDIA DGX Spark.
#
#   curl -fsSL https://raw.githubusercontent.com/Yunle-Lee/BotLan-CLI/main/install.sh | sh
#   sh install.sh [--usb <BotLan folder>] [--no-models] [--from-source] [--yes]
#
# What it does, in order (re-running it skips whatever is already done):
#   1. checks the machine (aarch64, NVIDIA GPU, CUDA 13 runtime, Python 3, disk)
#   2. gets the code (git clone of BotLan-CLI into ~/spark-duo)
#   3. gets the binaries: a BotLan USB stick if one is plugged in, else the prebuilt runtime from ModelScope
#      (Karoli/BotLan-Runtime-DGX-Spark), else builds llama.cpp from source (20-40 min)
#   4. asks whether to install BotLan's own models (Jev + StepFun GELab-Zero-4B, ~3.8 GB from ModelScope);
#      say no to use only a model server you already run, or an API
#   5. starts the stack and the BotLan gateway, then opens the setup wizard (first Bot, scope, model, budget)
set -eu

REPO_URL=${BOTLAN_REPO:-https://github.com/Yunle-Lee/BotLan-CLI}
DIR=${BOTLAN_DIR:-$HOME/spark-duo}
RUNTIME_MS=${BOTLAN_RUNTIME_MS:-Karoli/BotLan-Runtime-DGX-Spark}
RUNTIME_FILE=botlan-runtime-aarch64-cuda13.tar.gz
RUNTIME_SHA=564477aef1e2fb7f027ce69455090d764951897fa15e8a6884ff78a6233e53c2
LCBIN=$HOME/llama.cpp/build-cuda/bin
USB="" MODELS=ask FROM_SOURCE=0 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --usb) USB=$2; shift ;;
    --no-models) MODELS=no ;;
    --models) MODELS=yes ;;
    --from-source) FROM_SOURCE=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
# Reading answers from the terminal works even when the script itself comes through a pipe.
ask() {
  [ "$YES" = 1 ] && { echo "$2"; return; }
  if [ -r /dev/tty ]; then printf '%s [%s] ' "$1" "$2" > /dev/tty; read -r a < /dev/tty || a=""; else a=""; fi
  echo "${a:-$2}"
}

say "1/5 checking this machine"
[ "$(uname -m)" = aarch64 ] || warn "this is $(uname -m), not a DGX Spark (aarch64); prebuilt binaries will not run, building from source"
[ "$(uname -m)" = aarch64 ] || FROM_SOURCE=1
command -v nvidia-smi >/dev/null && nvidia-smi -L || die "no NVIDIA driver (nvidia-smi missing)"
command -v python3 >/dev/null || die "python3 missing"
command -v git >/dev/null || die "git missing"
CUDALIB=""
for d in ${CUDA_HOME:+$CUDA_HOME/targets/*/lib $CUDA_HOME/lib64} /usr/local/cuda/targets/*/lib /usr/local/cuda-13*/targets/*/lib /usr/local/cuda*/targets/*/lib /usr/local/cuda*/lib64; do
  [ -f "$d/libcudart.so.13" ] && [ -f "$d/libcublas.so.13" ] && { CUDALIB=$d; break; }
done
[ -z "$CUDALIB" ] && ldconfig -p 2>/dev/null | grep -q 'libcudart.so.13' && CUDALIB=system
if [ -z "$CUDALIB" ]; then warn "no CUDA 13 runtime found; building from source needs nvcc (CUDA toolkit)"; FROM_SOURCE=1; fi
echo "CUDA runtime: ${CUDALIB:-none}   python: $(python3 -V 2>&1)"
FREE_GB=$(df -Pk "$HOME" | awk 'NR==2{print int($4/1048576)}')
[ "$FREE_GB" -ge 12 ] || die "need ~12 GB free in $HOME, have ${FREE_GB} GB"
python3 -m pip --version >/dev/null 2>&1 || die "pip missing for python3 (DGX OS: it is preinstalled)"

# A plugged-in BotLan stick wins: no network needed.
if [ -z "$USB" ]; then
  for m in /media/"$USER"/*/BotLan /media/*/BotLan /mnt/*/BotLan /run/media/"$USER"/*/BotLan; do
    [ -f "$m/botlan-core.tar" ] && { USB=$m; break; }
  done
fi
[ -n "$USB" ] && echo "BotLan USB stick: $USB"

say "2/5 code → $DIR"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only || warn "could not update $DIR (local changes?); using it as is"
elif [ -f "$DIR/botlan_gateway.py" ]; then echo "using existing $DIR"
elif [ -n "$USB" ] && [ -f "$USB/BotLan-CLI-src.tar" ]; then tar -C "$(dirname "$DIR")" -xf "$USB/BotLan-CLI-src.tar" && mv "$(dirname "$DIR")/BotLan-CLI" "$DIR"
else git clone -q --depth 1 "$REPO_URL" "$DIR"; fi
cd "$DIR"

if [ -n "$USB" ]; then
  say "USB stick found: installing everything from it (offline)"
  exec sh scripts/00_offline_install.sh "$USB"
fi

say "3/5 binaries"
python3 -m pip install -q --user modelscope 2>/dev/null || python3 -m pip install -q --user --break-system-packages modelscope \
  || warn "could not install the modelscope CLI"
PATH=$HOME/.local/bin:$PATH
if [ -x "$LCBIN/llama-server" ] && [ -x bin/jev-score ] && [ -L lc-cuda/build/bin ]; then
  echo "already installed"
elif [ "$FROM_SOURCE" = 0 ] && command -v modelscope >/dev/null; then
  T=$HOME/.cache/botlan; mkdir -p "$T"
  modelscope download --model "$RUNTIME_MS" "$RUNTIME_FILE" --local_dir "$T"
  echo "$RUNTIME_SHA  $T/$RUNTIME_FILE" | sha256sum -c - || die "runtime download is damaged (checksum)"
  tar -C "$T" -xzf "$T/$RUNTIME_FILE"
  (cd "$T/botlan-offline" && sha256sum -c --quiet SHA256SUMS) || die "runtime bundle failed its checksums"
  mkdir -p "$LCBIN" bin lc-cuda/build
  cp -a "$T/botlan-offline/llama-bin/." "$LCBIN/"
  cp -a "$T/botlan-offline/jev-score" bin/jev-score && chmod +x bin/jev-score
  ln -sfn "$LCBIN" lc-cuda/build/bin
  if [ "$CUDALIB" = system ]; then : > .botlan-env
  else echo "export LD_LIBRARY_PATH=$CUDALIB\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}" > .botlan-env; fi
  . ./.botlan-env
  if ldd "$LCBIN/llama-server" bin/jev-score | grep -q 'not found'; then
    warn "prebuilt binaries cannot load here; building from source instead"; FROM_SOURCE=1
  else "$LCBIN/llama-server" --version 2>&1 | tail -1; fi
  python3 -c 'import tokenizers, numpy' 2>/dev/null || python3 -m pip install -q --user --no-index --find-links "$T/botlan-offline/wheels" tokenizers numpy \
    || python3 -m pip install -q --user --break-system-packages --no-index --find-links "$T/botlan-offline/wheels" tokenizers numpy
  python3 -m venv .venv 2>/dev/null && .venv/bin/pip install -q --no-index --find-links "$T/botlan-offline/wheels" textual || true
else
  FROM_SOURCE=1
fi
if [ "$FROM_SOURCE" = 1 ] && ! [ -x "$LCBIN/llama-server" ]; then
  say "building llama.cpp with CUDA (20-40 min)"
  [ -d "$HOME/llama.cpp/.git" ] || git clone -q https://github.com/ggml-org/llama.cpp "$HOME/llama.cpp"
  git -C "$HOME/llama.cpp" checkout -q 441df11f65ea0b6d0c72965aaf70c8241070ddcb
  sh scripts/01_build_llamacpp_cuda.sh
  python3 -m pip install -q --user tokenizers numpy 2>/dev/null || python3 -m pip install -q --user --break-system-packages tokenizers numpy
fi

say "4/5 models"
if [ "$MODELS" = ask ]; then
  echo "BotLan's own models: Jev-0.8B (skill router, 0.5 GB) + StepFun GELab-Zero-4B (vision-language, 3.3 GB)."
  echo "Without them, a Bot can still use a model server you already run here, or an external API."
  case "$(ask 'Install BotLan models? (Y/n)' Y)" in n|N|no|No) MODELS=no ;; *) MODELS=yes ;; esac
fi
if [ "$MODELS" = yes ]; then
  JEV=$HOME/models/Jev-Style-0.8B-Decision-v3-GGUF
  [ -f "$JEV/Jev-Style-0.8B-Decision-v3-Q4_K_M.gguf" ] \
    || modelscope download --model Karoli/Jev-Style-0.8B-Decision-v3-GGUF --local_dir "$JEV"
  # The prebuilt runtime already ships jev-score; only a source build needs to compile it.
  [ -x bin/jev-score ] || sh scripts/02_rebuild_jev_score.sh "$HOME/llama.cpp" || die "Jev scorer build failed"
  sh scripts/03_convert_gelab.sh "$HOME/llama.cpp"
fi

say "5/5 starting"
if [ "$MODELS" = yes ]; then sh scripts/04_serve.sh; fi
if [ "$MODELS" = yes ]; then sh scripts/08_botlan.sh --install; else BOTLAN_NO_MODELS=1 sh scripts/08_botlan.sh --install; fi
echo
echo "BotLan is installed in $DIR."
echo "Keep it running after you log out:  sudo loginctl enable-linger $USER"
if [ "$(ask 'Create your first Bot now? (Y/n)' Y)" != n ]; then
  if [ -r /dev/tty ]; then exec sh scripts/09_setup.sh < /dev/tty; else sh scripts/09_setup.sh; fi
fi
echo "later: sh $DIR/scripts/09_setup.sh   then on the laptop: BotLan tray menu → 连接 DGX Spark…"

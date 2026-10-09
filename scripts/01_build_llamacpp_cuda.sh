#!/bin/sh
# Build llama.cpp with native CUDA SASS for the DGX Spark GB10 (sm_121).
# Usage: sh 01_build_llamacpp_cuda.sh [llama.cpp-checkout]
#   env: CUDA_HOME (default /usr/local/cuda-13.0), ARCH (default 121), BUILD, JOBS
set -eu
LC=${1:-$HOME/llama.cpp}
CUDA=${CUDA_HOME:-/usr/local/cuda-13.0}
ARCH=${ARCH:-121}
BUILD=${BUILD:-$LC/build-cuda}
JOBS=${JOBS:-16}
[ -x "$CUDA/bin/nvcc" ] || { echo "no nvcc under $CUDA" >&2; exit 1; }
echo "checkout : $LC"
echo "cuda     : $CUDA"
echo "arch     : $ARCH"
echo "build    : $BUILD"
cmake -S "$LC" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=ON \
  -DGGML_CUDA=ON \
  -DGGML_NATIVE=ON \
  -DCMAKE_CUDA_ARCHITECTURES="$ARCH" \
  -DCMAKE_CUDA_COMPILER="$CUDA/bin/nvcc" \
  -DCUDAToolkit_ROOT="$CUDA" \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DCMAKE_BUILD_RPATH="$CUDA/targets/sbsa-linux/lib" \
  -DCMAKE_INSTALL_RPATH="$CUDA/targets/sbsa-linux/lib"
cmake --build "$BUILD" -j "$JOBS"
echo "OK: $BUILD/bin"

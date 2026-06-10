#!/usr/bin/env bash
# Build the optional native verification core and install the Python module
# into pipeline/refinement/ (where obligations.py auto-detects it).
#
#   ./core/build.sh            # configure + build + ctest + install module
#   PYTHON=python3.12 ./core/build.sh
#
# Requirements: cmake >= 3.18, a C++17 compiler. pybind11 (pip install
# pybind11) is needed only for the Python module — without it the library and
# its C++ tests still build, and the pipeline keeps using the pure-Python
# kernel.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.11}"

CMAKE_ARGS=(-S . -B build -DCMAKE_BUILD_TYPE=Release)
if PYBIND_DIR="$("$PYTHON" -m pybind11 --cmakedir 2>/dev/null)"; then
  CMAKE_ARGS+=("-Dpybind11_DIR=${PYBIND_DIR}")
  CMAKE_ARGS+=("-DPython_EXECUTABLE=$(command -v "$PYTHON")")
fi
if [[ "$(uname -s)" == "Darwin" ]]; then
  # Build for the arch the Python interpreter actually RUNS as — an Intel-brew
  # cmake under Rosetta would otherwise default the module to x86_64 and the
  # arm64 interpreter would refuse to load it.
  PYARCH="$("$PYTHON" -c 'import platform; print(platform.machine())')"
  CMAKE_ARGS+=("-DCMAKE_OSX_ARCHITECTURES=${PYARCH}")
fi

cmake "${CMAKE_ARGS[@]}"
cmake --build build -j
ctest --test-dir build --output-on-failure

shopt -s nullglob
sos=(build/_rtlcore*.so)
if ((${#sos[@]})); then
  cp "${sos[@]}" ../pipeline/refinement/
  echo "rtlcore: installed ${sos[*]##*/} -> pipeline/refinement/"
  (cd .. && "$PYTHON" - <<'EOF'
from pipeline.refinement.obligations import kernel_backend
from pipeline.cocotb.spec_sim import specsim_backend
print("rtlcore: obligations backend:", kernel_backend(),
      "| spec-sim backend:", specsim_backend())
EOF
  )
else
  echo "rtlcore: pybind11 not found — Python module skipped (C++ lib + tests built)"
fi

# rtlcore — native verification core for the agentic-rtl-engine.
#
# Builds:
#   rtlcore_lib   static library: compiled evaluator + obligation kernel
#   core_tests    zero-dependency unit-test runner (ctest)
#   _rtlcore      optional pybind11 module (skipped if pybind11 is absent);
#                 core/build.sh copies it into pipeline/refinement/ where
#                 obligations.py auto-detects it
#
# The Python pipeline NEVER hard-depends on this: obligations.py falls back to
# the pure-Python kernel when the module is not built.

cmake_minimum_required(VERSION 3.18)
project(rtlcore LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra)

add_library(rtlcore_lib STATIC
  src/expr.cpp
  src/obligations.cpp
  src/spec_sim.cpp
)
target_include_directories(rtlcore_lib PUBLIC include)
set_property(TARGET rtlcore_lib PROPERTY POSITION_INDEPENDENT_CODE ON)

# --- optional Python module -------------------------------------------------
find_package(Python COMPONENTS Interpreter Development.Module QUIET)
find_package(pybind11 CONFIG QUIET)
if(pybind11_FOUND AND Python_FOUND)
  pybind11_add_module(_rtlcore bindings/module.cpp)
  target_link_libraries(_rtlcore PRIVATE rtlcore_lib)
  message(STATUS "rtlcore: building the _rtlcore Python module")
else()
  message(STATUS
    "rtlcore: pybind11/Python not found — _rtlcore module skipped "
    "(library + tests still build)")
endif()

# --- tests --------------------------------------------------------------------
enable_testing()
add_executable(core_tests tests/test_core.cpp)
target_link_libraries(core_tests PRIVATE rtlcore_lib)
add_test(NAME core_tests COMMAND core_tests)

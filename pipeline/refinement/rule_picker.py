# DEPRECATED — do not add code here.
#
# The Rule Picker responsibility was consolidated into Agent 3
# (pipeline/agents/agent3.py) per the Version A decision in
# docs/handoff_runtime_agents.md §4.
#
# Agent 3 exposes a one-shot pick_rule(applicable_rules, spec) call that
# the Refinement Engine invokes as an injected callable. The engine never
# imports this file.
#
# See: pipeline/refinement/engine.py::run() — pick_rule parameter.

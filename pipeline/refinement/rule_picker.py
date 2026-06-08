# DEPRECATED — do not add code here.
#
# The Rule Picker responsibility was consolidated into Agent 3
# (pipeline/agents/agent3.py) as the one-shot pick_rule call type.
# See docs/agents.md (Agent 3) and docs/refinement.md.
#
# Agent 3 exposes a one-shot pick_rule(applicable_rules, spec) call that
# the Refinement Engine invokes as an injected callable. The engine never
# imports this file.
#
# See: pipeline/refinement/engine.py::run() — pick_rule parameter.

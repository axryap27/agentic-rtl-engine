Agentic RTL generation and verification using LangGraph

 LangGraph acts as the orchestrator. A StateGraph is a DAG where each node is a Python function (state) -> state. The compiled graph runs nodes, evaluates conditional edges after each one to decide where to go next (retry, advance, or halt), and tracks state between hops. 
                                       
  In this engine, state is intentionally thin — just run_id, retry_counts, halt. The actual data (TLA+ specs, Verilog, evaluation results) lives on disk as JSON artifacts. Each stage:
  1. Reads the previous stage's JSON artifact from artifacts/<run_id>/  
  2. Does work (calls Claude, runs a tool, etc.)                                                             
  3. Writes its own JSON artifact 
  4. Returns the updated state dict
                                   
  The conditional edges after each node then read status from the freshly-written artifact to decide: retry this stage, advance, or halt.

  What each stage does:             
  - Stage 1 — Claude reads the NL description, outputs a TLA+ formal spec (.tla + .cfg) and metadata JSON
  - Stage 2 — (still a stub) Applies refinement rules to produce a PlusCal implementation with bsv_mapping fields that tell Stage 3 exactly how to map abstract variables to hardware primitives
  - Stage 3 — Claude reads the PlusCal + bsv_mapping fields, generates synthesizable Verilog-2001, then lints it
  - Stage 4 — (still a stub) Runs benchmarks, synthesizes with Yosys, writes an evaluation report 
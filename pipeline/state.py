"""
LangGraph pipeline state.

Kept intentionally thin — no design data here. Design data travels exclusively
via artifact files on disk (artifacts/<run_id>/).

run_id        — unique identifier for this pipeline run; used to locate artifacts/
retry_counts  — per-stage retry counter, keyed by stage label e.g. "stage1_tlc"
halt          — set to True by any node that wants to stop the graph immediately
"""

from typing import TypedDict


class PipelineState(TypedDict):
    run_id: str
    retry_counts: dict[str, int]
    halt: bool

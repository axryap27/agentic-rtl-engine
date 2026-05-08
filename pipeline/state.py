from typing import TypedDict


class PipelineState(TypedDict):
    run_id: str
    retry_counts: dict[str, int]
    halt: bool

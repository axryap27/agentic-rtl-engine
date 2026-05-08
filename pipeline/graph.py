from langgraph.graph import END, StateGraph

from pipeline.nodes.stage1 import stage1_node
from pipeline.nodes.stage2 import stage2_node
from pipeline.nodes.stage3 import stage3_node
from pipeline.nodes.stage4 import stage4_node
from pipeline.state import PipelineState


def build_graph():
    workflow = StateGraph(PipelineState)

    workflow.add_node("stage1", stage1_node)
    workflow.add_node("stage2", stage2_node)
    workflow.add_node("stage3", stage3_node)
    workflow.add_node("stage4", stage4_node)

    workflow.set_entry_point("stage1")
    workflow.add_edge("stage1", "stage2")
    workflow.add_edge("stage2", "stage3")
    workflow.add_edge("stage3", "stage4")
    workflow.add_edge("stage4", END)

    return workflow.compile()

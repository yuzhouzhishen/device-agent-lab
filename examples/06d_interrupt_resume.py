from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class ApprovalState(TypedDict, total=False):
    operation: str
    status: str


def request_approval(state: ApprovalState) -> ApprovalState:
    approved = interrupt(
        {
            "question": f"是否执行：{state['operation']}？",
            "operation": state["operation"],
        }
    )
    return {"status": "executed" if approved else "cancelled"}


def build_graph():
    builder = StateGraph(ApprovalState)
    builder.add_node("request_approval", request_approval)
    builder.add_edge(START, "request_approval")
    builder.add_edge("request_approval", END)
    return builder.compile(checkpointer=InMemorySaver())


def run_demo(approved: bool) -> tuple[dict[str, Any], ApprovalState]:
    graph = build_graph()
    config = {"configurable": {"thread_id": "06d-learning-demo"}}
    paused = graph.invoke({"operation": "打开2号端口"}, config=config)
    completed = graph.invoke(Command(resume=approved), config=config)
    return paused, completed


if __name__ == "__main__":
    paused_state, completed_state = run_demo(approved=True)
    request = paused_state["__interrupt__"][0].value
    print("paused:")
    print(json.dumps(request, ensure_ascii=False, indent=2))
    print("\nresume with: True")
    print(json.dumps(completed_state, ensure_ascii=False, indent=2))

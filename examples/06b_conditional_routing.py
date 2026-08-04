from __future__ import annotations

import json
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class RoutingState(TypedDict, total=False):
    request_type: Literal["query", "control"]
    result: str


def route_request(state: RoutingState) -> RoutingState:
    return {}


def choose_branch(state: RoutingState) -> Literal["query", "control"]:
    return state["request_type"]


def query(state: RoutingState) -> RoutingState:
    return {"result": "进入查询分支"}


def control(state: RoutingState) -> RoutingState:
    return {"result": "进入控制分支"}


def build_graph():
    builder = StateGraph(RoutingState)
    builder.add_node("route_request", route_request)
    builder.add_node("query", query)
    builder.add_node("control", control)
    builder.add_edge(START, "route_request")
    builder.add_conditional_edges("route_request", choose_branch)
    builder.add_edge("query", END)
    builder.add_edge("control", END)
    return builder.compile()


def run_demo(request_type: Literal["query", "control"]) -> RoutingState:
    graph = build_graph()
    return graph.invoke({"request_type": request_type})


if __name__ == "__main__":
    print("query input:")
    print(json.dumps(run_demo("query"), ensure_ascii=False, indent=2))
    print("\ncontrol input:")
    print(json.dumps(run_demo("control"), ensure_ascii=False, indent=2))

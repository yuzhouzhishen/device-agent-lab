from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class BasicState(TypedDict):
    number: int


def add_one(state: BasicState) -> BasicState:
    return {"number": state["number"] + 1}


def build_graph():
    builder = StateGraph(BasicState)
    builder.add_node("add_one", add_one)
    builder.add_edge(START, "add_one")
    builder.add_edge("add_one", END)
    return builder.compile()


def run_demo() -> BasicState:
    graph = build_graph()
    return graph.invoke({"number": 1})


if __name__ == "__main__":
    print("START -> add_one -> END")
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))

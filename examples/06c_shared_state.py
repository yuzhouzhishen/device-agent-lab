from __future__ import annotations

import json
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


class SharedState(TypedDict, total=False):
    port_text: str
    port_id: int
    result: str
    summary: str
    trace: Annotated[list[str], operator.add]


def normalize(state: SharedState) -> SharedState:
    return {
        "port_id": int(state["port_text"].strip()),
        "trace": ["normalize"],
    }


def query(state: SharedState) -> SharedState:
    return {
        "result": f"port {state['port_id']}: standby",
        "trace": ["query"],
    }


def summarize(state: SharedState) -> SharedState:
    return {
        "summary": f"查询完成：{state['result']}",
        "trace": ["summarize"],
    }


def build_graph():
    builder = StateGraph(SharedState)
    builder.add_node("normalize", normalize)
    builder.add_node("query", query)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "normalize")
    builder.add_edge("normalize", "query")
    builder.add_edge("query", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile()


def run_demo() -> SharedState:
    graph = build_graph()
    return graph.invoke({"port_text": " 2 ", "trace": []})


if __name__ == "__main__":
    print("START -> normalize -> query -> summarize -> END")
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))

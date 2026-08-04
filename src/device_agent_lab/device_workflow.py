from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    validate_command_against_context,
)
from device_agent_lab.command_executor import execute_device_command
from device_agent_lab.mock_device import DeviceController


class DeviceWorkflowState(TypedDict, total=False):
    command: dict[str, Any]
    context: dict[str, Any]
    result: dict[str, Any]
    summary: str
    trace: Annotated[list[str], operator.add]


def build_device_workflow(controller: DeviceController):
    """Build the explicit LangGraph device workflow."""

    def validate(state: DeviceWorkflowState) -> DeviceWorkflowState:
        command = DeviceCommand.model_validate(state["command"])
        context = DeviceAgentContext.model_validate(state["context"])
        validate_command_against_context(command, context)
        return {"trace": ["validate"]}

    def query(state: DeviceWorkflowState) -> DeviceWorkflowState:
        command = DeviceCommand.model_validate(state["command"])
        context = DeviceAgentContext.model_validate(state["context"])
        result = execute_device_command(command, context, controller)
        return {"result": result, "trace": ["query"]}

    def control(state: DeviceWorkflowState) -> DeviceWorkflowState:
        command = DeviceCommand.model_validate(state["command"])
        context = DeviceAgentContext.model_validate(state["context"])
        approved = interrupt(
            {
                "type": "device_control_confirmation",
                "question": command.reason,
                "command": command.model_dump(),
            }
        )
        if not approved:
            return {
                "result": {
                    "ok": False,
                    "action": command.action,
                      "cancelled": True,
                    "message": "control command was cancelled",
                },
                "trace": ["control"],
            }
        result = execute_device_command(
            command,
            context,
            controller,
            confirmed=True,
        )
        return {"result": result, "trace": ["control"]}

    def clarify(state: DeviceWorkflowState) -> DeviceWorkflowState:
        command = DeviceCommand.model_validate(state["command"])
        context = DeviceAgentContext.model_validate(state["context"])
        result = execute_device_command(command, context, controller)
        return {"result": result, "trace": ["clarify"]}

    def route_command(
        state: DeviceWorkflowState,
    ) -> Literal["query", "control", "clarify"]:
        command = DeviceCommand.model_validate(state["command"])
        if command.action == "set_port_power":
            return "control"
        if command.action == "ask_clarification":
            return "clarify"
        return "query"

    def summarize(state: DeviceWorkflowState) -> DeviceWorkflowState:
        command = DeviceCommand.model_validate(state["command"])
        port = state["result"].get("port")
        summary = "设备查询已完成。"
        if state["result"].get("cancelled"):
            summary = "控制操作已取消。"
        if state["result"].get("action") == "ask_clarification":
            summary = state["result"]["message"]
        if port is not None:
            summary = f"端口 {port['port_id']} 当前状态为 {port['mode']}。"
        device = state["result"].get("device")
        if (
            command.action == "set_port_power"
            and command.port is not None
            and device is not None
        ):
            port_state = device["ports"][str(command.port)]
            summary = f"端口 {command.port} 已更新为 {port_state['mode']}。"
        return {"summary": summary, "trace": ["summarize"]}

    builder = StateGraph(DeviceWorkflowState)
    builder.add_node("validate", validate)
    builder.add_node("query", query)
    builder.add_node("control", control)
    builder.add_node("clarify", clarify)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "validate")
    builder.add_conditional_edges("validate", route_command)
    builder.add_edge("query", "summarize")
    builder.add_edge("control", "summarize")
    builder.add_edge("clarify", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(checkpointer=InMemorySaver())

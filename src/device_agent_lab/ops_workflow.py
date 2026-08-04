from __future__ import annotations

import operator
from collections.abc import Awaitable
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    validate_command_against_context,
)
from device_agent_lab.device_gateway import DeviceGateway, DeviceGatewayError
from device_agent_lab.knowledge_base import LocalKnowledgeBase


class DeviceOpsState(TypedDict, total=False):
    conversation_id: str
    request: str
    command: dict[str, Any]
    context: dict[str, Any]
    knowledge: list[dict[str, Any]]
    diagnostic_port: dict[str, Any]
    diagnostic_charging: dict[str, Any]
    diagnostic_pd: dict[str, Any]
    diagnostic_temperature: dict[str, Any]
    control_before: dict[str, Any]
    result: dict[str, Any]
    summary: str
    trace: Annotated[list[str], operator.add]


def build_ops_workflow(
    gateway: DeviceGateway,
    knowledge_base: LocalKnowledgeBase | None = None,
):
    """Build the application workflow against a replaceable device backend."""
    retriever = knowledge_base or LocalKnowledgeBase()

    def validate(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        context = DeviceAgentContext.model_validate(state["context"])
        validate_command_against_context(command, context)
        return {"trace": ["validate"]}

    def retrieve(state: DeviceOpsState) -> DeviceOpsState:
        return {
            "knowledge": retriever.search(state["request"]),
            "trace": ["retrieve"],
        }

    async def query(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        try:
            if command.action == "get_status":
                result = {
                    "ok": True,
                    "action": command.action,
                    "device": await gateway.get_status(),
                }
            elif command.action == "get_port_status" and command.port is not None:
                result = {
                    "ok": True,
                    "action": command.action,
                    "port": await gateway.get_port_status(command.port),
                }
            else:
                raise ValueError(f"unsupported query action: {command.action}")
        except (DeviceGatewayError, TimeoutError) as exc:
            result = _failure_result(command.action, exc)
        return {"result": result, "trace": ["query"]}

    async def collect_port_status(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        if command.port is None:
            raise ValueError("diagnose_port requires port")
        return {
            "diagnostic_port": await _capture_evidence(
                gateway.get_port_status(command.port)
            ),
            "trace": ["collect_port_status"],
        }

    async def collect_charging_status(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        return {
            "diagnostic_charging": await _capture_evidence(
                gateway.get_charging_status()
            ),
            "trace": ["collect_charging_status"],
        }

    async def collect_pd_status(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        return {
            "diagnostic_pd": await _capture_evidence(
                gateway.get_port_pd_status()
            ),
            "trace": ["collect_pd_status"],
        }

    async def collect_temperature_mode(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        return {
            "diagnostic_temperature": await _capture_evidence(
                gateway.get_temperature_mode()
            ),
            "trace": ["collect_temperature_mode"],
        }

    def analyze_diagnosis(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        if command.port is None:
            raise ValueError("diagnose_port requires port")
        return {
            "result": _build_port_diagnosis(command.port, state),
            "trace": ["analyze_diagnosis"],
        }

    async def prepare_control(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        if command.port is None:
            raise ValueError("set_port_power requires port")
        before = await _capture_evidence(
            gateway.get_port_status(command.port)
        )
        output: DeviceOpsState = {
            "control_before": before,
            "trace": ["prepare_control"],
        }
        if before.get("status") != "ok":
            output["result"] = {
                "ok": False,
                "action": command.action,
                "stage": "precheck",
                "error_type": before.get("error_type", "UnknownError"),
                "message": (
                    "无法读取操作前端口状态，已阻止控制命令："
                    + str(before.get("message", "unknown error"))
                ),
            }
        return output

    async def control(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        approved = interrupt(
            {
                "type": "device_control_confirmation",
                "question": command.reason,
                "command": command.model_dump(),
                "before": state["control_before"]["data"],
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
        if command.port is None or command.enabled is None:
            raise ValueError("set_port_power requires port and enabled")
        try:
            result = await gateway.set_port_power(
                command.port,
                enabled=command.enabled,
            )
        except (DeviceGatewayError, TimeoutError) as exc:
            result = _failure_result(command.action, exc)
        result.setdefault("action", command.action)
        return {"result": result, "trace": ["control"]}

    async def verify_control(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        if command.port is None or command.enabled is None:
            raise ValueError("set_port_power requires port and enabled")
        command_result = state["result"]
        after = await _capture_evidence(
            gateway.get_port_status(command.port)
        )
        before = state["control_before"]
        if after.get("status") != "ok":
            verification = {
                "status": "error",
                "expected_enabled": command.enabled,
                "state_changed": None,
                "message": (
                    "控制命令已返回，但无法读取操作后状态："
                    + str(after.get("message", "unknown error"))
                ),
            }
        else:
            verification = _verify_port_control(
                enabled=command.enabled,
                before=before["data"],
                after=after["data"],
            )
        return {
            "result": {
                "ok": bool(command_result.get("ok", False)),
                "action": command.action,
                "message": str(
                    command_result.get(
                        "message",
                        "port control command completed",
                    )
                ),
                "before": before,
                "command_result": command_result,
                "after": after,
                "verification": verification,
            },
            "trace": ["verify_control"],
        }

    def clarify(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        return {
            "result": {
                "ok": False,
                "action": command.action,
                "message": command.reason,
            },
            "trace": ["clarify"],
        }

    def route_command(
        state: DeviceOpsState,
    ) -> Literal[
        "query",
        "collect_port_status",
        "prepare_control",
        "clarify",
    ]:
        command = DeviceCommand.model_validate(state["command"])
        if command.action == "set_port_power":
            return "prepare_control"
        if command.action == "diagnose_port":
            return "collect_port_status"
        if command.action == "ask_clarification":
            return "clarify"
        return "query"

    def route_prepared_control(
        state: DeviceOpsState,
    ) -> Literal["control", "summarize"]:
        if state["control_before"].get("status") == "ok":
            return "control"
        return "summarize"

    def route_control_result(
        state: DeviceOpsState,
    ) -> Literal["verify_control", "summarize"]:
        result = state["result"]
        if result.get("ok") and not result.get("cancelled"):
            return "verify_control"
        return "summarize"

    def summarize(state: DeviceOpsState) -> DeviceOpsState:
        result = state["result"]
        command = DeviceCommand.model_validate(state["command"])
        if not result.get("ok") and not result.get("cancelled"):
            summary = f"设备操作失败：{result.get('message', 'unknown error')}"
        elif result.get("cancelled"):
            summary = "控制操作已取消。"
        elif result.get("action") == "ask_clarification":
            summary = str(result["message"])
        elif result.get("action") == "diagnose_port":
            diagnosis = result["diagnosis"]
            summary = str(diagnosis["conclusion"])
            findings = diagnosis.get("findings", [])
            if findings:
                summary = f"{summary}\n诊断证据：" + "；".join(findings[:3])
        elif "port" in result:
            summary = _summarize_port_status(
                result["port"],
                detail=command.response_detail,
            )
        elif result.get("action") == "set_port_power":
            verification = result.get("verification")
            if verification is None:
                summary = str(result.get("message", "端口控制已完成。"))
            else:
                summary = str(verification["message"])
        else:
            summary = _summarize_device_status(
                result["device"],
                request=state["request"],
                focus=command.response_focus,
                detail=command.response_detail,
            )
        knowledge = state.get("knowledge", [])
        if (
            knowledge
            and result.get("ok")
            and command.response_detail != "concise"
            and command.response_focus
            not in {"charging_ports", "connected_ports"}
            and result.get("action")
            in {"get_status", "get_port_status", "diagnose_port"}
        ):
            first = knowledge[0]
            summary = (
                f"{summary}\n参考排查《{first['title']}》：{first['content']}"
            )
        return {"summary": summary, "trace": ["summarize"]}

    builder = StateGraph(DeviceOpsState)
    builder.add_node("validate", validate)
    builder.add_node("retrieve", retrieve)
    builder.add_node("query", query)
    builder.add_node("collect_port_status", collect_port_status)
    builder.add_node(
        "collect_charging_status",
        collect_charging_status,
    )
    builder.add_node("collect_pd_status", collect_pd_status)
    builder.add_node(
        "collect_temperature_mode",
        collect_temperature_mode,
    )
    builder.add_node("analyze_diagnosis", analyze_diagnosis)
    builder.add_node("prepare_control", prepare_control)
    builder.add_node("control", control)
    builder.add_node("verify_control", verify_control)
    builder.add_node("clarify", clarify)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "retrieve")
    builder.add_conditional_edges("retrieve", route_command)
    builder.add_edge("query", "summarize")
    builder.add_edge(
        "collect_port_status",
        "collect_charging_status",
    )
    builder.add_edge(
        "collect_charging_status",
        "collect_pd_status",
    )
    builder.add_edge(
        "collect_pd_status",
        "collect_temperature_mode",
    )
    builder.add_edge(
        "collect_temperature_mode",
        "analyze_diagnosis",
    )
    builder.add_edge("analyze_diagnosis", "summarize")
    builder.add_conditional_edges(
        "prepare_control",
        route_prepared_control,
    )
    builder.add_conditional_edges(
        "control",
        route_control_result,
    )
    builder.add_edge("verify_control", "summarize")
    builder.add_edge("clarify", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(checkpointer=InMemorySaver())


def _failure_result(action: str, error: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "error_type": type(error).__name__,
        "message": str(error),
    }


def _summarize_device_status(
    device: dict[str, Any],
    *,
    request: str = "",
    focus: str = "device_overview",
    detail: str = "standard",
) -> str:
    ports = [
        port
        for port in device.get("ports", {}).values()
        if isinstance(port, dict)
    ]
    charging = [
        port
        for port in ports
        if port.get("mode") == "charging"
    ]
    connected = [
        port
        for port in ports
        if bool(port.get("connected", False))
    ]
    if focus == "charging_ports":
        return _summarize_selected_ports(
            charging,
            request=request,
            detail=detail,
            state_label="正在充电",
            empty_message="当前没有端口在充电。",
        )
    if focus == "connected_ports":
        return _summarize_selected_ports(
            connected,
            request=request,
            detail=detail,
            state_label="已连接设备",
            empty_message="当前没有端口连接设备。",
        )
    prefix = (
        f"设备在线，共 {len(ports)} 个端口；"
        if device.get("online", True)
        else f"设备离线，共识别到 {len(ports)} 个端口；"
    )
    if not charging:
        return prefix + "当前没有端口在充电。"
    if len(charging) == 1:
        port = charging[0]
        return (
            prefix
            + f"当前 {port['port_id']} 号端口正在充电，"
            + f"输出 {float(port.get('power_w', 0)):.2f} W"
            + _format_electrical_details(port)
            + "。"
        )
    details = "、".join(
        f"{port['port_id']} 号 {float(port.get('power_w', 0)):.2f} W"
        for port in charging
    )
    total_power = sum(
        float(port.get("power_w", 0))
        for port in charging
    )
    return (
        prefix
        + f"当前 {len(charging)} 个端口正在充电：{details}，"
        + f"合计 {total_power:.2f} W。"
    )


def _summarize_selected_ports(
    ports: list[dict[str, Any]],
    *,
    request: str,
    detail: str,
    state_label: str,
    empty_message: str,
) -> str:
    if not ports:
        return empty_message
    normalized = request.lower()
    if any(
        phrase in normalized
        for phrase in ("几个", "多少个", "how many")
    ):
        return f"{len(ports)}个端口{state_label}。"
    port_names = "、".join(
        f"{int(port['port_id'])}号"
        for port in ports
    )
    if detail == "concise":
        return f"{port_names}端口。"
    if detail == "detailed":
        details = "；".join(
            (
                f"{int(port['port_id'])}号端口{state_label}，"
                f"{float(port.get('power_w', 0)):.2f} W"
                + _format_electrical_details(port)
            )
            for port in ports
        )
        return details + "。"
    return f"{port_names}端口{state_label}。"


def _summarize_port_status(
    port: dict[str, Any],
    *,
    detail: str = "standard",
) -> str:
    port_id = port["port_id"]
    power_w = float(port.get("power_w", 0))
    if detail == "concise":
        if port.get("mode") == "charging":
            return f"{port_id}号端口正在充电。"
        if port.get("connected"):
            return f"{port_id}号端口已连接，但未在充电。"
        return f"{port_id}号端口未连接。"
    if port.get("mode") == "charging":
        return (
            f"{port_id} 号端口正在充电，输出 {power_w:.2f} W"
            + _format_electrical_details(port)
            + "。"
        )
    if port.get("connected"):
        return (
            f"{port_id} 号端口已连接设备，但当前输出仅 {power_w:.2f} W，"
            "尚未进入稳定充电状态。"
        )
    return f"{port_id} 号端口当前没有连接负载，也没有充电。"


def _format_electrical_details(port: dict[str, Any]) -> str:
    details = (
        f"（{float(port.get('voltage_v', 0)):.3f} V / "
        f"{float(port.get('current_a', 0)):.3f} A"
    )
    protocol = port.get("protocol")
    if protocol and protocol != "Not Charging":
        details += f"，{protocol}"
    return details + "）"


def _verify_port_control(
    *,
    enabled: bool,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    state_changed = any(
        before.get(field) != after.get(field)
        for field in (
            "mode",
            "connected",
            "voltage_v",
            "current_a",
            "power_w",
        )
    )
    if enabled:
        expected_state = (
            after.get("mode") in {"charging", "standby"}
            or bool(after.get("connected", False))
            or float(after.get("power_w", 0)) > 0.5
        )
    else:
        expected_state = (
            after.get("mode") == "off"
            and not bool(after.get("connected", False))
            and float(after.get("power_w", 0)) <= 0.5
        )

    if expected_state and state_changed:
        status = "verified"
        message = "控制命令已执行，操作后状态变化符合预期。"
    elif expected_state:
        status = "already_satisfied"
        message = "控制命令已执行，操作前端口状态已经符合目标。"
    elif not bool(after.get("connected", False)):
        status = "unobservable"
        message = (
            "设备已接受控制命令，但端口当前没有连接负载，"
            "现有遥测无法确认使能状态变化。"
        )
    else:
        status = "mismatch"
        message = "控制命令已返回，但操作后状态与目标不一致。"

    return {
        "status": status,
        "expected_enabled": enabled,
        "state_changed": state_changed,
        "message": message,
    }


async def _capture_evidence(
    operation: Awaitable[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return {
            "status": "ok",
            "data": await operation,
        }
    except (DeviceGatewayError, TimeoutError, ValueError) as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


def _build_port_diagnosis(
    port_id: int,
    state: DeviceOpsState,
) -> dict[str, Any]:
    evidence = {
        "port_status": state.get(
            "diagnostic_port",
            {"status": "missing"},
        ),
        "charging_status": state.get(
            "diagnostic_charging",
            {"status": "missing"},
        ),
        "pd_status": state.get(
            "diagnostic_pd",
            {"status": "missing"},
        ),
        "temperature_mode": state.get(
            "diagnostic_temperature",
            {"status": "missing"},
        ),
    }
    unavailable = [
        name
        for name, item in evidence.items()
        if item.get("status") != "ok"
    ]
    port_evidence = evidence["port_status"]
    if port_evidence.get("status") != "ok":
        return {
            "ok": False,
            "action": "diagnose_port",
            "diagnosis": {
                "port_id": port_id,
                "health": "unknown",
                "conclusion": (
                    f"无法读取端口 {port_id} 的基础状态，暂时不能完成诊断。"
                ),
                "findings": [
                    f"{name} 证据不可用"
                    for name in unavailable
                ],
                "recommendations": [
                    "确认设备在线并检查 MCP 连接后重试。",
                ],
                "evidence": evidence,
            },
        }

    port = port_evidence["data"]
    charging_data = evidence["charging_status"].get("data", {})
    charging_ports = charging_data.get("charging_ports", {})
    marked_charging = bool(
        charging_ports.get(f"port_{port_id}", False)
    )
    pd_ports = evidence["pd_status"].get("data", {}).get("ports", [])
    pd_port = next(
        (
            item
            for item in pd_ports
            if int(item.get("port", -1)) == port_id
        ),
        None,
    )
    temperature = evidence["temperature_mode"].get("data", {})

    findings = [
        (
            f"端口状态={port.get('mode', 'unknown')}，"
            f"connected={bool(port.get('connected', False))}"
        ),
        (
            f"实时输出={float(port.get('voltage_v', 0)):.3f} V / "
            f"{float(port.get('current_a', 0)):.3f} A / "
            f"{float(port.get('power_w', 0)):.2f} W"
        ),
        f"充电位图标记={'是' if marked_charging else '否'}",
    ]
    if temperature:
        findings.append(
            "温度策略="
            + str(temperature.get("mode_name", "unknown"))
        )
    if pd_port is None:
        findings.append("目标端口没有可用的 PD 协商记录")
    elif "error" in pd_port:
        findings.append(f"PD 状态读取失败：{pd_port['error']}")
    else:
        findings.append(
            "PD 协议="
            + str(pd_port.get("pd_revision", "unknown"))
        )
    findings.extend(
        f"{name} 证据不可用"
        for name in unavailable
    )

    connected = bool(port.get("connected", False))
    power_w = float(port.get("power_w", 0))
    if not connected:
        health = "disconnected"
        conclusion = (
            f"端口 {port_id} 未检测到连接设备，因此当前没有充电和 "
            "PD 协商。"
        )
        recommendations = [
            "检查线缆两端是否插紧并重新插拔。",
            "更换已知正常的线缆或受电设备后再次诊断。",
        ]
    elif power_w > 0.5 and marked_charging:
        health = "healthy"
        conclusion = (
            f"端口 {port_id} 已建立连接并正在输出功率，当前充电链路正常。"
        )
        recommendations = [
            "如功率仍不符合预期，检查受电设备需求和 PD 协商档位。",
        ]
    elif marked_charging:
        health = "attention"
        conclusion = (
            f"端口 {port_id} 被标记为充电，但实时输出功率接近 0 W，"
            "需要继续检查线缆、受电设备或 PD 协商。"
        )
        recommendations = [
            "重新插拔线缆并观察 PD 协商是否恢复。",
            "使用已知正常的受电设备进行交叉验证。",
        ]
    else:
        health = "attention"
        conclusion = (
            f"端口 {port_id} 检测到连接，但设备没有将其标记为充电，"
            "可能尚未完成协议协商。"
        )
        recommendations = [
            "检查 PD 协商记录和线缆能力。",
            "更换线缆或受电设备后重新诊断。",
        ]

    return {
        "ok": True,
        "action": "diagnose_port",
        "diagnosis": {
            "port_id": port_id,
            "health": health,
            "conclusion": conclusion,
            "findings": findings,
            "recommendations": recommendations,
            "evidence": evidence,
        },
    }

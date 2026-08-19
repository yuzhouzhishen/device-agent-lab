from __future__ import annotations

import asyncio
import operator
import re
from collections.abc import Awaitable
from typing import Annotated, Any, Literal, TypedDict

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from device_agent_lab.agent_contracts import (
    DeviceConversationContext,
    DeviceAgentContext,
    DeviceCommand,
    validate_command_against_context,
)
from device_agent_lab.device_gateway import DeviceGateway, DeviceGatewayError
from device_agent_lab.knowledge_gateway import (
    GeneralKnowledgeProvider,
    KnowledgeGateway,
    KnowledgeGatewayError,
    LocalKnowledgeGateway,
    citations_support_specific_request,
    is_public_general_knowledge_request,
    is_specific_device_knowledge_request,
)
from device_agent_lab.knowledge_base import LocalKnowledgeBase


class DeviceOpsState(TypedDict, total=False):
    conversation_id: str
    conversation: dict[str, Any]
    request: str
    command: dict[str, Any]
    context: dict[str, Any]
    knowledge: list[dict[str, Any]]
    knowledge_answer: str
    knowledge_metadata: dict[str, Any]
    warnings: Annotated[list[str], operator.add]
    diagnostic_device: dict[str, Any]
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
    knowledge_gateway: KnowledgeGateway | None = None,
    general_knowledge_provider: GeneralKnowledgeProvider | None = None,
    checkpointer: Any | None = None,
    control_verify_delay_seconds: float = 0.0,
    control_verify_attempts: int = 1,
):
    """Build the application workflow against a replaceable device backend."""
    if knowledge_gateway is not None and knowledge_base is not None:
        raise ValueError(
            "provide knowledge_gateway or knowledge_base, not both"
        )
    if control_verify_delay_seconds < 0:
        raise ValueError(
            "control_verify_delay_seconds must not be negative"
        )
    if control_verify_attempts <= 0:
        raise ValueError("control_verify_attempts must be positive")
    knowledge = knowledge_gateway or LocalKnowledgeGateway(knowledge_base)

    def validate(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        context = DeviceAgentContext.model_validate(state["context"])
        validate_command_against_context(command, context)
        return {"trace": ["validate"]}

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

    async def collect_device_status(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        del state
        return {
            "diagnostic_device": await _capture_evidence(
                gateway.get_status()
            ),
            "trace": ["collect_device_status"],
        }

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

    def analyze_device_diagnosis(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        return {
            "result": _build_device_diagnosis(state),
            "trace": ["analyze_device_diagnosis"],
        }

    async def retrieve_knowledge(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        try:
            answer = await knowledge.answer(
                _build_knowledge_query(state),
                top_k=4,
            )
        except (KnowledgeGatewayError, TimeoutError) as exc:
            return {
                "knowledge": [],
                "knowledge_metadata": {
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                },
                "warnings": [
                    "知识服务不可用，本次仅依据实时设备证据完成诊断。"
                ],
                "trace": ["retrieve_knowledge"],
            }
        output: DeviceOpsState = {
            "knowledge": [
                citation.model_dump() for citation in answer.citations
            ],
            "knowledge_answer": answer.answer,
            "knowledge_metadata": {
                "status": answer.status,
                "origin": "knowledge_base",
                "retriever": answer.retriever,
                "reranker": answer.reranker,
                "generation_mode": answer.generation_mode,
                "degraded": answer.degraded,
                "latency_ms": answer.latency_ms,
                "trace": answer.trace,
            },
            "trace": ["retrieve_knowledge"],
        }
        if answer.degraded:
            output["warnings"] = [
                "知识服务的生成模型不可用，已返回检索式降级答案。"
            ]
        return output

    def respond_chat(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        return {
            "result": {
                "ok": True,
                "action": command.action,
                "message": command.reply
                or (
                    "你好，我是 DeviceOps。你可以让我查询、诊断或"
                    "控制设备，也可以询问固件知识。"
                ),
                "response_kind": "conversation",
            },
            "trace": ["respond_chat"],
        }

    async def answer_knowledge(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        try:
            answer = await knowledge.answer(
                state["request"],
                top_k=4,
            )
        except (KnowledgeGatewayError, TimeoutError) as exc:
            return {
                "result": {
                    "ok": False,
                    "action": "answer_knowledge",
                    "message": "固件知识服务当前不可用，请稍后重试。",
                    "error_type": type(exc).__name__,
                },
                "knowledge": [],
                "knowledge_metadata": {
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                },
                "warnings": ["知识服务不可用，本次未生成知识答案。"],
                "trace": ["answer_knowledge"],
            }
        if (
            answer.status == "answered"
            and is_specific_device_knowledge_request(state["request"])
            and not citations_support_specific_request(
                state["request"],
                answer.citations,
            )
        ):
            answer = answer.model_copy(
                update={
                    "status": "no_evidence",
                    "answer": (
                        "当前知识库没有找到能直接证明该设备能力的证据，"
                        "暂不回答。"
                    ),
                    "citations": [],
                    "trace": [
                        *answer.trace,
                        "specific_capability_evidence_guard",
                    ],
                    "generation_mode": "none",
                }
            )
        if (
            answer.status == "no_evidence"
            and general_knowledge_provider is not None
            and is_public_general_knowledge_request(state["request"])
        ):
            try:
                general_answer = await general_knowledge_provider.answer(
                    state["request"]
                )
            except (httpx.HTTPError, TypeError, ValueError, TimeoutError):
                general_answer = None
            if general_answer is not None:
                return {
                    "result": {
                        "ok": True,
                        "action": "answer_knowledge",
                        "status": "answered",
                        "response_kind": "general_knowledge",
                    },
                    "knowledge": [],
                    "knowledge_answer": general_answer.answer,
                    "knowledge_metadata": {
                        "status": "general_knowledge",
                        "origin": "general_model",
                        "retriever": "none",
                        "reranker": "none",
                        "generation_mode": "ollama-general",
                        "model": general_answer.model,
                        "degraded": True,
                        "latency_ms": answer.latency_ms,
                        "trace": [
                            *answer.trace,
                            "general_knowledge_fallback",
                        ],
                    },
                    "warnings": [
                        "该回答来自模型通用知识，未经当前知识库验证。"
                    ],
                    "trace": [
                        "answer_knowledge",
                        "general_knowledge_fallback",
                    ],
                }

        output: DeviceOpsState = {
            "result": {
                "ok": True,
                "action": "answer_knowledge",
                "status": answer.status,
                "origin": "knowledge_base",
                "response_kind": "knowledge",
            },
            "knowledge": [
                citation.model_dump() for citation in answer.citations
            ],
            "knowledge_answer": answer.answer,
            "knowledge_metadata": {
                "status": answer.status,
                "origin": "knowledge_base",
                "retriever": answer.retriever,
                "reranker": answer.reranker,
                "generation_mode": answer.generation_mode,
                "degraded": answer.degraded,
                "latency_ms": answer.latency_ms,
                "trace": answer.trace,
            },
            "trace": ["answer_knowledge"],
        }
        if answer.degraded:
            output["warnings"] = [
                "知识服务的生成模型不可用，已返回检索式降级答案。"
            ]
        return output

    def explain_previous(
        state: DeviceOpsState,
    ) -> DeviceOpsState:
        conversation = DeviceConversationContext.model_validate(
            state.get("conversation", {})
        )
        if not conversation.recent_turns:
            return {
                "result": {
                    "ok": True,
                    "action": "ask_clarification",
                    "message": "当前会话里还没有可以解释的上一条结果。",
                    "needs_clarification": True,
                },
                "trace": ["explain_previous"],
            }
        turn = conversation.recent_turns[-1]
        action_label = {
            "respond_chat": "业务对话",
            "answer_knowledge": "固件知识查询",
            "get_status": "整机状态查询",
            "get_port_status": "端口状态查询",
            "diagnose_device": "整机诊断",
            "diagnose_port": "端口诊断",
            "set_port_power": "端口控制",
            "ask_clarification": "信息澄清",
        }.get(turn.action, turn.action)
        parts = [
            f"上一条请求执行的是“{action_label}”。",
            f"当时的结论是：{turn.summary}",
        ]
        if turn.evidence:
            parts.append(
                "这个结论来自已保存的真实证据："
                + "；".join(turn.evidence[:5])
            )
        if turn.knowledge_titles:
            parts.append(
                "参考知识：" + "、".join(turn.knowledge_titles[:3])
            )
        elif turn.knowledge_origin == "general_model":
            parts.append(
                "回答来源：Ollama 通用知识，未经当前知识库验证。"
            )
        return {
            "result": {
                "ok": True,
                "action": "explain_previous",
                "message": "\n".join(parts),
                "response_kind": "explanation",
            },
            "trace": ["explain_previous"],
        }

    def unsupported(state: DeviceOpsState) -> DeviceOpsState:
        command = DeviceCommand.model_validate(state["command"])
        return {
            "result": {
                "ok": True,
                "action": command.action,
                "message": command.reply
                or (
                    "这个请求超出了当前设备运维范围。"
                    "我可以处理设备状态、端口控制、充电诊断以及"
                    "固件和协议知识问题。"
                ),
                "response_kind": "unsupported",
            },
            "trace": ["unsupported"],
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
        before = state["control_before"]
        after: dict[str, Any] = {"status": "missing"}
        verification: dict[str, Any] = {}
        for attempt in range(1, control_verify_attempts + 1):
            if control_verify_delay_seconds:
                await asyncio.sleep(control_verify_delay_seconds)
            after = await _capture_evidence(
                gateway.get_port_status(command.port)
            )
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
            verification["attempts"] = attempt
            if verification["status"] in {
                "verified",
                "already_satisfied",
            }:
                break
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
                "ok": True,
                "action": command.action,
                "message": command.reason,
                "needs_clarification": True,
                "response_kind": "clarification",
            },
            "trace": ["clarify"],
        }

    def route_command(
        state: DeviceOpsState,
    ) -> Literal[
        "query",
        "collect_device_status",
        "collect_port_status",
        "prepare_control",
        "respond_chat",
        "answer_knowledge",
        "explain_previous",
        "unsupported",
        "clarify",
    ]:
        command = DeviceCommand.model_validate(state["command"])
        if command.action == "respond_chat":
            return "respond_chat"
        if command.action == "answer_knowledge":
            return "answer_knowledge"
        if command.action == "explain_previous":
            return "explain_previous"
        if command.action == "unsupported_request":
            return "unsupported"
        if command.action == "set_port_power":
            return "prepare_control"
        if command.action == "diagnose_device":
            return "collect_device_status"
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
        if result.get("action") in {
            "respond_chat",
            "explain_previous",
            "unsupported_request",
            "ask_clarification",
        }:
            summary = str(result["message"])
        elif result.get("action") == "answer_knowledge":
            summary = str(
                state.get("knowledge_answer")
                or result.get("message")
                or "固件知识库没有找到足够证据。"
            )
        elif not result.get("ok") and not result.get("cancelled"):
            summary = f"设备操作失败：{result.get('message', 'unknown error')}"
        elif result.get("cancelled"):
            summary = "控制操作已取消。"
        elif result.get("action") == "diagnose_port":
            diagnosis = result["diagnosis"]
            summary = str(diagnosis["conclusion"])
            findings = diagnosis.get("findings", [])
            if findings:
                summary = f"{summary}\n诊断证据：" + "；".join(findings[:3])
        elif result.get("action") == "diagnose_device":
            diagnosis = result["diagnosis"]
            summary = str(diagnosis["conclusion"])
            findings = diagnosis.get("findings", [])
            if findings:
                summary = f"{summary}\n设备证据：" + "；".join(findings[:4])
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
        retrieved_knowledge = state.get("knowledge", [])
        knowledge_answer = state.get("knowledge_answer", "")
        if (
            retrieved_knowledge
            and knowledge_answer
            and result.get("ok")
            and command.response_detail != "concise"
            and result.get("action")
            in {"diagnose_device", "diagnose_port"}
        ):
            titles = "、".join(
                f"《{title}》"
                for title in dict.fromkeys(
                    str(item["title"])
                    for item in retrieved_knowledge
                )
            )
            summary = (
                f"{summary}\n知识库建议：{knowledge_answer}"
                f"\n参考来源：{titles}"
            )
        return {"summary": summary, "trace": ["summarize"]}

    builder = StateGraph(DeviceOpsState)
    builder.add_node("validate", validate)
    builder.add_node("query", query)
    builder.add_node("collect_device_status", collect_device_status)
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
    builder.add_node(
        "analyze_device_diagnosis",
        analyze_device_diagnosis,
    )
    builder.add_node("retrieve_knowledge", retrieve_knowledge)
    builder.add_node("respond_chat", respond_chat)
    builder.add_node("answer_knowledge", answer_knowledge)
    builder.add_node("explain_previous", explain_previous)
    builder.add_node("unsupported", unsupported)
    builder.add_node("prepare_control", prepare_control)
    builder.add_node("control", control)
    builder.add_node("verify_control", verify_control)
    builder.add_node("clarify", clarify)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "validate")
    builder.add_conditional_edges("validate", route_command)
    builder.add_edge("respond_chat", "summarize")
    builder.add_edge("answer_knowledge", "summarize")
    builder.add_edge("explain_previous", "summarize")
    builder.add_edge("unsupported", "summarize")
    builder.add_edge("query", "summarize")
    builder.add_edge(
        "collect_device_status",
        "collect_charging_status",
    )
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
    builder.add_conditional_edges(
        "collect_temperature_mode",
        lambda state: (
            "analyze_device_diagnosis"
            if DeviceCommand.model_validate(state["command"]).action
            == "diagnose_device"
            else "analyze_diagnosis"
        ),
    )
    builder.add_edge("analyze_diagnosis", "retrieve_knowledge")
    builder.add_edge(
        "analyze_device_diagnosis",
        "retrieve_knowledge",
    )
    builder.add_edge("retrieve_knowledge", "summarize")
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
    return builder.compile(
        checkpointer=checkpointer or InMemorySaver()
    )


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
        findings.append(
            "PD 能力不匹配="
            + (
                "是"
                if bool(
                    pd_port.get(
                        "request_capability_mismatch",
                        False,
                    )
                )
                else "否"
            )
        )
    findings.extend(
        f"{name} 证据不可用"
        for name in unavailable
    )

    connected = bool(port.get("connected", False))
    power_w = float(port.get("power_w", 0))
    capability_mismatch = bool(
        pd_port
        and pd_port.get("request_capability_mismatch", False)
    )
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
    elif power_w > 0.5 and marked_charging and capability_mismatch:
        health = "attention"
        conclusion = (
            f"端口 {port_id} 已经输出功率，但 PD 状态报告能力不匹配，"
            "这可能解释实际功率低于用户预期。"
        )
        recommendations = [
            "核对 Source Capabilities 与 Sink Request 的电压、电流和 PDO 档位。",
            "检查线缆 e-Marker、电流能力和整机功率预算。",
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


def _build_device_diagnosis(
    state: DeviceOpsState,
) -> dict[str, Any]:
    evidence = {
        "device_status": state.get(
            "diagnostic_device",
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
    device_evidence = evidence["device_status"]
    if device_evidence.get("status") != "ok":
        return {
            "ok": False,
            "action": "diagnose_device",
            "diagnosis": {
                "health": "unknown",
                "conclusion": "无法读取设备整体状态，暂时不能完成诊断。",
                "findings": [
                    f"{name} 证据不可用" for name in unavailable
                ],
                "recommendations": [
                    "确认设备在线并检查 MCP 连接后重试。",
                ],
                "evidence": evidence,
                "suspect_ports": [],
            },
        }

    device = device_evidence["data"]
    ports = [
        port
        for port in device.get("ports", {}).values()
        if isinstance(port, dict)
    ]
    connected = [
        port for port in ports if bool(port.get("connected", False))
    ]
    charging = [
        port
        for port in connected
        if port.get("mode") == "charging"
        and float(port.get("power_w", 0)) > 0.5
    ]
    unstable_ports = [
        int(port["port_id"])
        for port in connected
        if (
            port.get("mode") != "charging"
            or float(port.get("power_w", 0)) <= 0.5
        )
    ]
    pd_ports = evidence["pd_status"].get("data", {}).get("ports", [])
    mismatch_ports = [
        int(port["port"])
        for port in pd_ports
        if (
            isinstance(port, dict)
            and "port" in port
            and bool(port.get("request_capability_mismatch", False))
        )
    ]
    suspect_ports = sorted(set(unstable_ports + mismatch_ports))
    findings = [
        f"设备在线={bool(device.get('online', True))}",
        f"端口总数={len(ports)}，已连接={len(connected)}，充电中={len(charging)}",
    ]
    if connected:
        findings.append(
            "实时总输出="
            + f"{sum(float(port.get('power_w', 0)) for port in connected):.2f} W"
        )
    temperature = evidence["temperature_mode"].get("data", {})
    if temperature:
        findings.append(
            "温度策略="
            + str(temperature.get("mode_name", "unknown"))
        )
    if mismatch_ports:
        findings.append(
            "PD 能力不匹配端口="
            + "、".join(str(port) for port in mismatch_ports)
            + " 号"
        )
    findings.extend(
        f"{name} 证据不可用" for name in unavailable
    )

    if suspect_ports:
        health = "attention"
        conclusion = (
            "设备在线，但检测到已连接且没有稳定输出的端口："
            + "、".join(str(port) for port in suspect_ports)
            + " 号。"
        )
        recommendations = [
            "对可疑端口逐一执行端口诊断并检查 PD 协商。",
            "结合线材、受电设备和温控状态进行交叉验证。",
        ]
    elif unavailable:
        health = "partial"
        conclusion = (
            "当前可用遥测未发现明确异常，但部分诊断证据不可用。"
        )
        recommendations = [
            "恢复缺失的 MCP 查询能力后再次执行完整诊断。",
        ]
    else:
        health = "healthy"
        conclusion = "设备在线，当前遥测未发现明确异常。"
        recommendations = [
            "如用户仍感知异常，请指定端口和现象进行定向诊断。",
        ]

    return {
        "ok": True,
        "action": "diagnose_device",
        "device": device,
        "diagnosis": {
            "health": health,
            "conclusion": conclusion,
            "findings": findings,
            "recommendations": recommendations,
            "evidence": evidence,
            "suspect_ports": suspect_ports,
        },
    }


def _build_knowledge_query(state: DeviceOpsState) -> str:
    result = state.get("result", {})
    diagnosis = result.get("diagnosis", {})
    request = _generalize_port_reference(
        str(state.get("request", ""))
    )
    if diagnosis.get("health") == "healthy":
        return "\n".join(
            [
                "用户问题：" + request,
                "检索主题：充电设备固件验收 设备状态 端口功率 "
                "温度策略 PD 协商 健康巡检。",
                "当前设备已由实时遥测判定健康。请仅给出文档中"
                "适用于后续巡检的观察项，不要假设当前存在故障。",
            ]
        )
    lines = [
        "请仅依据固件知识库和下方已确认的实时证据给出建议。",
        "用户问题：" + request,
        "实时结论："
        + _generalize_port_reference(
            str(diagnosis.get("conclusion", ""))
        ),
        f"实时健康度：{diagnosis.get('health', 'unknown')}",
    ]
    findings = diagnosis.get("findings", [])
    if findings:
        lines.append(
            "实时证据：" + "；".join(str(item) for item in findings[:6])
        )
    lines.append(
        "重点说明与实时证据一致的可能原因、下一步验证方式和操作风险。"
    )
    return "\n".join(lines)


def _generalize_port_reference(value: str) -> str:
    generalized = re.sub(
        r"\d+\s*号?\s*端口|端口\s*\d+",
        "目标端口",
        value,
    )
    return re.sub(r"\s+", " ", generalized).strip()

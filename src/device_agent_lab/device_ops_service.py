from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from langgraph.types import Command
from pydantic import BaseModel, Field

from device_agent_lab.agent_contracts import (
    ConversationTurn,
    DeviceAgentContext,
    DeviceCommand,
    DeviceConversationContext,
)
from device_agent_lab.audit import AuditLog, AuditRecord, NullAuditLog
from device_agent_lab.device_gateway import DeviceGateway
from device_agent_lab.knowledge_base import LocalKnowledgeBase
from device_agent_lab.ops_workflow import build_ops_workflow
from device_agent_lab.planner import CommandPlanner


class DeviceOpsRun(BaseModel):
    thread_id: str
    conversation_id: str
    status: Literal["completed", "confirmation_required"]
    request: str
    command: DeviceCommand
    confirmation: dict[str, Any] | None = None
    approved: bool | None = None
    result: dict[str, Any] | None = None
    summary: str | None = None
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list)


class DeviceOpsService:
    """Public application interface for planning and running device requests."""

    def __init__(
        self,
        *,
        planner: CommandPlanner,
        gateway: DeviceGateway,
        knowledge_base: LocalKnowledgeBase | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._planner = planner
        self._audit_log = audit_log or NullAuditLog()
        self._conversations: dict[str, DeviceConversationContext] = {}
        self._graph = build_ops_workflow(
            gateway,
            knowledge_base=knowledge_base,
        )

    async def start(
        self,
        request: str,
        context: DeviceAgentContext,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
    ) -> DeviceOpsRun:
        current_thread = thread_id or str(uuid4())
        current_conversation = conversation_id or current_thread
        conversation = self._conversations.setdefault(
            current_conversation,
            DeviceConversationContext(),
        )
        command = await self._planner.plan(
            request,
            context,
            conversation,
        )
        state = await self._graph.ainvoke(
            {
                "conversation_id": current_conversation,
                "request": request,
                "command": command.model_dump(),
                "context": context.model_dump(),
                "trace": [],
            },
            config=_config(current_thread),
        )
        run = _build_run(current_thread, state)
        self._remember(run)
        self._write_audit(run)
        return run

    async def resume(
        self,
        thread_id: str,
        *,
        approved: bool,
    ) -> DeviceOpsRun:
        state = await self._graph.ainvoke(
            Command(resume=approved),
            config=_config(thread_id),
        )
        run = _build_run(thread_id, state, approved=approved)
        self._remember(run)
        self._write_audit(run)
        return run

    def _remember(self, run: DeviceOpsRun) -> None:
        if run.status != "completed":
            return
        conversation = self._conversations.setdefault(
            run.conversation_id,
            DeviceConversationContext(),
        )
        result = run.result or {}
        port = _result_port(result)
        if result.get("device") is not None:
            charging_ports = _charging_ports(result["device"])
            conversation.charging_ports = charging_ports
            conversation.last_port = (
                charging_ports[0]
                if len(charging_ports) == 1
                else None
            )
        elif port is not None:
            conversation.last_port = int(port["port_id"])
            charging = set(conversation.charging_ports)
            if port.get("mode") == "charging":
                charging.add(conversation.last_port)
            else:
                charging.discard(conversation.last_port)
            conversation.charging_ports = sorted(charging)
        elif run.command.port is not None:
            conversation.last_port = run.command.port

        conversation.recent_turns.append(
            ConversationTurn(
                request=run.request,
                summary=run.summary or "",
                action=run.command.action,
                port=run.command.port,
            )
        )
        conversation.recent_turns = conversation.recent_turns[-6:]

    def _write_audit(self, run: DeviceOpsRun) -> None:
        self._audit_log.append(
            AuditRecord(
                thread_id=run.thread_id,
                conversation_id=run.conversation_id,
                status=run.status,
                request=run.request,
                command=run.command.model_dump(),
                approved=run.approved,
                result=run.result,
                trace=run.trace,
                knowledge_ids=[
                    str(document["id"])
                    for document in run.knowledge
                    if "id" in document
                ],
            )
        )


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _build_run(
    thread_id: str,
    state: dict[str, Any],
    *,
    approved: bool | None = None,
) -> DeviceOpsRun:
    interrupts = state.get("__interrupt__", ())
    confirmation = interrupts[0].value if interrupts else None
    status = "confirmation_required" if confirmation else "completed"
    return DeviceOpsRun(
        thread_id=thread_id,
        conversation_id=state.get("conversation_id", thread_id),
        status=status,
        request=state["request"],
        command=DeviceCommand.model_validate(state["command"]),
        confirmation=confirmation,
        approved=approved,
        result=state.get("result"),
        summary=state.get("summary"),
        knowledge=state.get("knowledge", []),
        trace=state.get("trace", []),
    )


def _result_port(result: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(result.get("port"), dict):
        return result["port"]
    after = result.get("after")
    if (
        isinstance(after, dict)
        and after.get("status") == "ok"
        and isinstance(after.get("data"), dict)
    ):
        return after["data"]
    diagnosis = result.get("diagnosis")
    if isinstance(diagnosis, dict):
        evidence = diagnosis.get("evidence", {}).get("port_status", {})
        if (
            evidence.get("status") == "ok"
            and isinstance(evidence.get("data"), dict)
        ):
            return evidence["data"]
    return None


def _charging_ports(device: dict[str, Any]) -> list[int]:
    return sorted(
        int(port["port_id"])
        for port in device.get("ports", {}).values()
        if isinstance(port, dict) and port.get("mode") == "charging"
    )

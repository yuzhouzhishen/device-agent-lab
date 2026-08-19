from __future__ import annotations

from collections.abc import AsyncIterator
from time import perf_counter
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
from device_agent_lab.conversation_store import (
    ConversationStore,
    InMemoryConversationStore,
)
from device_agent_lab.device_gateway import DeviceGateway
from device_agent_lab.knowledge_gateway import (
    GeneralKnowledgeProvider,
    KnowledgeGateway,
)
from device_agent_lab.knowledge_base import LocalKnowledgeBase
from device_agent_lab.metrics import (
    RunMetrics,
    StepTiming,
    TokenPricing,
    TokenUsage,
)
from device_agent_lab.ops_workflow import build_ops_workflow
from device_agent_lab.planner import CommandPlanner


class FollowUpSuggestion(BaseModel):
    """A safe, executable next question grounded in the completed run."""

    label: str
    request: str
    kind: Literal["query", "diagnosis", "explanation", "knowledge"]
    reason: str


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
    knowledge_answer: str | None = None
    knowledge_metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list)
    suggestions: list[FollowUpSuggestion] = Field(default_factory=list)
    metrics: RunMetrics | None = None


class DeviceOpsEvent(BaseModel):
    """One progress notification emitted while a workflow runs."""

    type: Literal["trace", "run", "error"]
    node: str | None = None
    step: str | None = None
    duration_ms: float | None = None
    run: DeviceOpsRun | None = None
    detail: str | None = None
    error_type: str | None = None


class DeviceOpsService:
    """Public application interface for planning and running device requests."""

    def __init__(
        self,
        *,
        planner: CommandPlanner,
        gateway: DeviceGateway,
        knowledge_base: LocalKnowledgeBase | None = None,
        knowledge_gateway: KnowledgeGateway | None = None,
        general_knowledge_provider: GeneralKnowledgeProvider | None = None,
        audit_log: AuditLog | None = None,
        conversation_store: ConversationStore | None = None,
        conversation_namespace: str = "",
        pricing: TokenPricing | None = None,
        control_verify_delay_seconds: float = 0.0,
        control_verify_attempts: int = 1,
    ) -> None:
        self._planner = planner
        self._audit_log = audit_log or NullAuditLog()
        self._pricing = pricing
        self._conversation_store = (
            conversation_store or InMemoryConversationStore()
        )
        self._conversation_namespace = conversation_namespace
        self._conversations: dict[str, DeviceConversationContext] = {}
        self._graph = build_ops_workflow(
            gateway,
            knowledge_base=knowledge_base,
            knowledge_gateway=knowledge_gateway,
            general_knowledge_provider=general_knowledge_provider,
            control_verify_delay_seconds=control_verify_delay_seconds,
            control_verify_attempts=control_verify_attempts,
        )

    async def start(
        self,
        request: str,
        context: DeviceAgentContext,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
    ) -> DeviceOpsRun:
        return await _drain(
            self.start_stream(
                request,
                context,
                thread_id=thread_id,
                conversation_id=conversation_id,
            )
        )

    async def resume(
        self,
        thread_id: str,
        *,
        approved: bool,
    ) -> DeviceOpsRun:
        return await _drain(
            self.resume_stream(thread_id, approved=approved)
        )

    async def start_stream(
        self,
        request: str,
        context: DeviceAgentContext,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[DeviceOpsEvent]:
        """Run a request, emitting each workflow step before the final run."""
        started = perf_counter()
        current_thread = thread_id or str(uuid4())
        current_conversation = conversation_id or current_thread
        conversation = self._conversation(current_conversation)
        plan = await self._planner.plan(request, context, conversation)
        planner_ms = _elapsed_ms(started)
        graph_input = {
            "conversation_id": current_conversation,
            "conversation": conversation.model_dump(),
            "request": request,
            "command": plan.command.model_dump(),
            "context": context.model_dump(),
            "trace": [],
            "warnings": (
                [plan.warning]
                if plan.degraded and plan.warning is not None
                else []
            ),
        }
        async for event in self._stream(
            current_thread,
            graph_input,
            started=started,
            planner_ms=planner_ms,
            usage=plan.usage,
            model=plan.model,
        ):
            yield event

    async def resume_stream(
        self,
        thread_id: str,
        *,
        approved: bool,
    ) -> AsyncIterator[DeviceOpsEvent]:
        """Resume a pending control request with step-by-step progress."""
        # Resuming replays no planning, so there is no model cost to attribute.
        async for event in self._stream(
            thread_id,
            Command(resume=approved),
            started=perf_counter(),
            approved=approved,
        ):
            yield event

    async def _stream(
        self,
        thread_id: str,
        graph_input: Any,
        *,
        started: float,
        planner_ms: float = 0.0,
        usage: TokenUsage | None = None,
        model: str | None = None,
        approved: bool | None = None,
    ) -> AsyncIterator[DeviceOpsEvent]:
        config = _config(thread_id)
        interrupts: tuple[Any, ...] = ()
        steps: list[StepTiming] = []
        workflow_started = perf_counter()
        previous = workflow_started
        async for chunk in self._graph.astream(
            graph_input,
            config=config,
            stream_mode="updates",
        ):
            for node, update in (chunk or {}).items():
                if node == "__interrupt__":
                    interrupts = tuple(update or ())
                    continue
                # Time since the previous node finished, so graph overhead is
                # attributed rather than silently dropped.
                step_ms = _elapsed_ms(previous)
                previous = perf_counter()
                for step in _update_trace(update):
                    steps.append(
                        StepTiming(node=str(node), duration_ms=step_ms)
                    )
                    yield DeviceOpsEvent(
                        type="trace",
                        node=str(node),
                        step=step,
                        duration_ms=step_ms,
                    )

        workflow_ms = _elapsed_ms(workflow_started)
        # `updates` only carries per-node deltas, so read the authoritative
        # final state back from the checkpointer to build the run.
        snapshot = await self._graph.aget_state(config)
        state = dict(snapshot.values or {})
        if interrupts:
            state["__interrupt__"] = interrupts
        run = _build_run(thread_id, state, approved=approved)
        run.metrics = self._build_metrics(
            duration_ms=_elapsed_ms(started),
            planner_ms=planner_ms,
            workflow_ms=workflow_ms,
            steps=steps,
            usage=usage,
            model=model,
        )
        run.suggestions = _build_follow_up_suggestions(run)
        self._remember(run)
        self._write_audit(run)
        yield DeviceOpsEvent(type="run", run=run)

    def _build_metrics(
        self,
        *,
        duration_ms: float,
        planner_ms: float,
        workflow_ms: float,
        steps: list[StepTiming],
        usage: TokenUsage | None,
        model: str | None,
    ) -> RunMetrics:
        pricing = self._pricing
        chargeable = pricing is not None and usage is not None
        return RunMetrics(
            duration_ms=duration_ms,
            planner_ms=planner_ms,
            workflow_ms=workflow_ms,
            steps=steps,
            usage=usage,
            cost=pricing.cost_of(usage) if chargeable else None,
            currency=pricing.currency if chargeable else None,
            model=model,
        )

    def _remember(self, run: DeviceOpsRun) -> None:
        if run.status != "completed":
            return
        conversation = self._conversation(run.conversation_id)
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
                intent=run.command.intent,
                evidence=_memory_evidence(result),
                knowledge_titles=list(
                    dict.fromkeys(
                        str(document["title"])
                        for document in run.knowledge
                        if document.get("title")
                    )
                )[:4],
                knowledge_origin=str(
                    run.knowledge_metadata.get("origin", "")
                ),
                trace=run.trace[-8:],
            )
        )
        conversation.recent_turns = conversation.recent_turns[-8:]
        self._conversation_store.save(
            self._conversation_store_id(run.conversation_id),
            conversation,
        )

    def _conversation(
        self,
        conversation_id: str,
    ) -> DeviceConversationContext:
        if conversation_id not in self._conversations:
            self._conversations[conversation_id] = (
                self._conversation_store.load(
                    self._conversation_store_id(conversation_id)
                )
                or DeviceConversationContext()
            )
        return self._conversations[conversation_id]

    def _conversation_store_id(self, conversation_id: str) -> str:
        if not self._conversation_namespace:
            return conversation_id
        return f"{self._conversation_namespace}:{conversation_id}"

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


def _elapsed_ms(since: float) -> float:
    return round((perf_counter() - since) * 1000, 2)


async def _drain(events: AsyncIterator[DeviceOpsEvent]) -> DeviceOpsRun:
    """Consume a run stream and return its terminal run event."""
    async for event in events:
        if event.type == "run" and event.run is not None:
            return event.run
    raise RuntimeError("workflow stream ended without a run event")


def _update_trace(update: Any) -> list[str]:
    if not isinstance(update, dict):
        return []
    trace = update.get("trace")
    if not isinstance(trace, list):
        return []
    return [str(step) for step in trace]


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
        knowledge_answer=state.get("knowledge_answer"),
        knowledge_metadata=state.get("knowledge_metadata", {}),
        warnings=state.get("warnings", []),
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


def _build_follow_up_suggestions(
    run: DeviceOpsRun,
) -> list[FollowUpSuggestion]:
    """Derive supported next questions from structured execution state."""
    result = run.result or {}
    if (
        run.status != "completed"
        or result.get("ok") is False
        or result.get("cancelled")
        or result.get("needs_clarification")
    ):
        return []

    suggestions: list[FollowUpSuggestion] = []
    seen: set[str] = set()

    def add(
        label: str,
        request: str,
        kind: Literal["query", "diagnosis", "explanation", "knowledge"],
        reason: str,
    ) -> None:
        if request in seen or len(suggestions) >= 3:
            return
        seen.add(request)
        suggestions.append(
            FollowUpSuggestion(
                label=label,
                request=request,
                kind=kind,
                reason=reason,
            )
        )

    action = run.command.action
    port = run.command.port
    if port is None:
        result_port = _result_port(result)
        if result_port is not None and result_port.get("port_id") is not None:
            port = int(result_port["port_id"])

    if action == "get_port_status" and port is not None:
        add(
            f"诊断 {port} 号端口",
            f"诊断 {port} 号端口当前是否存在充电异常",
            "diagnosis",
            "继续诊断本轮明确查询的端口",
        )
        add(
            "解释本次结果",
            "刚才为什么这么判断？",
            "explanation",
            "解释本轮端口状态的真实设备证据",
        )
        add(
            "查看设备整体状态",
            "查询设备整体状态",
            "query",
            "从端口状态扩展到整机视角",
        )
    elif action == "diagnose_port" and port is not None:
        add(
            f"查看 {port} 号端口状态",
            f"查询 {port} 号端口当前状态",
            "query",
            "复查本轮诊断端口的实时状态",
        )
        add(
            "解释诊断依据",
            "刚才为什么这么判断？",
            "explanation",
            "展开本轮诊断使用的设备与知识证据",
        )
        add(
            "查看设备整体状态",
            "查询设备整体状态",
            "query",
            "确认端口问题是否影响整机",
        )
    elif action == "set_port_power" and port is not None:
        add(
            f"查看 {port} 号端口状态",
            f"查询 {port} 号端口当前状态",
            "query",
            "复查本轮控制目标的最新状态",
        )
        add(
            "解释控制结果",
            "刚才的控制结果和验证依据是什么？",
            "explanation",
            "解释控制后轮询验证证据",
        )
        add(
            "查看设备整体状态",
            "查询设备整体状态",
            "query",
            "确认控制后的整机状态",
        )
    elif action == "diagnose_device":
        diagnosis = result.get("diagnosis")
        suspect_ports = (
            diagnosis.get("suspect_ports", [])
            if isinstance(diagnosis, dict)
            else []
        )
        if suspect_ports:
            suspect = int(suspect_ports[0])
            add(
                f"诊断 {suspect} 号端口",
                f"诊断 {suspect} 号端口当前是否存在充电异常",
                "diagnosis",
                "整机诊断将该端口标记为可疑",
            )
        add(
            "解释诊断依据",
            "刚才为什么这么判断？",
            "explanation",
            "展开本轮整机诊断证据",
        )
        add(
            "查看当前充电端口",
            "现在几号端口在充电",
            "query",
            "继续查看整机诊断中的充电范围",
        )
    elif action == "get_status":
        device = result.get("device")
        charging = _charging_ports(device) if isinstance(device, dict) else []
        if run.command.response_focus == "charging_ports" and len(charging) == 1:
            active_port = charging[0]
            add(
                f"查看 {active_port} 号端口状态",
                f"查询 {active_port} 号端口当前状态",
                "query",
                "本轮结果只识别到一个充电端口",
            )
        add(
            "检查设备是否异常",
            "帮我检查设备当前有没有异常",
            "diagnosis",
            "基于本轮整机状态继续诊断",
        )
        if run.command.response_focus != "charging_ports":
            add(
                "查看当前充电端口",
                "现在几号端口在充电",
                "query",
                "聚焦整机状态中的充电端口",
            )
        add(
            "解释本次结果",
            "刚才为什么这么判断？",
            "explanation",
            "解释本轮整机查询证据",
        )
    elif action == "answer_knowledge":
        add(
            "查看回答依据",
            "刚才的回答依据是什么？",
            "explanation",
            "展开本轮知识来源",
        )
        add(
            "检查设备当前状态",
            "查询设备整体状态",
            "query",
            "将知识结论与实时设备状态结合",
        )
    elif action == "respond_chat":
        add(
            "查看当前充电端口",
            "现在几号端口在充电",
            "query",
            "进入只读设备查询",
        )
        add(
            "检查设备是否异常",
            "帮我检查设备当前有没有异常",
            "diagnosis",
            "进入设备诊断流程",
        )

    return suggestions


def _memory_evidence(result: dict[str, Any]) -> list[str]:
    diagnosis = result.get("diagnosis")
    if isinstance(diagnosis, dict):
        return [
            str(finding)
            for finding in diagnosis.get("findings", [])
            if finding
        ][:4]

    verification = result.get("verification")
    if isinstance(verification, dict):
        return [
            f"控制验证={verification.get('status', 'unknown')}",
            str(verification.get("message", "")),
        ]

    port = _result_port(result)
    if port is not None:
        return [
            f"端口={port.get('port_id', '-')}",
            f"模式={port.get('mode', 'unknown')}",
            (
                "连接=是"
                if port.get("connected", False)
                else "连接=否"
            ),
            f"功率={port.get('power_w', '-')}W",
            f"协议={port.get('protocol', 'none')}",
        ]

    device = result.get("device")
    if isinstance(device, dict):
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
        return [
            f"设备在线={bool(device.get('online', False))}",
            f"端口总数={len(ports)}",
            f"充电端口数={len(charging)}",
        ]

    return []

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DeviceAgentContext(BaseModel):
    """Runtime context that limits what the Agent is allowed to control."""

    device_id: str = Field(description="Current device id or PSN.")
    online: bool = Field(description="Whether the current device is online.")
    allowed_ports: list[int] = Field(description="Ports that exist on the current device.")
    operator_role: str = Field(description="The current user's role in this demo.")
    safety_policy: str = Field(
        default="控制类操作必须先给出结构化动作，并等待业务代码确认后再执行。",
        description="Safety rule the Agent must follow.",
    )


class DeviceCommand(BaseModel):
    """Structured response produced by the Agent before any device action runs."""

    intent: Literal[
        "chat",
        "knowledge_query",
        "query_device",
        "control_device",
        "clarify",
        "unsupported",
    ] = Field(
        description="High-level user intent."
    )
    action: Literal[
        "respond_chat",
        "answer_knowledge",
        "explain_previous",
        "unsupported_request",
        "get_status",
        "get_port_status",
        "diagnose_device",
        "diagnose_port",
        "set_port_power",
        "ask_clarification",
    ] = Field(description="Next device action requested by the Agent.")
    device_id: str = Field(description="Target device id.")
    port: int | None = Field(
        default=None,
        description="Target port id. Required for port-level query and control.",
    )
    enabled: bool | None = Field(
        default=None,
        description="Whether the port should be enabled. Required for set_port_power.",
    )
    response_focus: Literal[
        "auto",
        "device_overview",
        "charging_ports",
        "connected_ports",
        "port_status",
        "diagnosis",
        "control_result",
    ] = Field(
        default="auto",
        description="Which verified result fields should be emphasized in the answer.",
    )
    response_detail: Literal["concise", "standard", "detailed"] = Field(
        default="standard",
        description="How much verified detail the final answer should include.",
    )
    need_confirmation: bool = Field(
        description="Whether business code should ask for confirmation before execution."
    )
    reason: str = Field(description="Short Chinese explanation for the selected action.")
    reply: str | None = Field(
        default=None,
        description=(
            "A concise grounded reply for conversation-only actions. "
            "Device and knowledge results are composed after tools run."
        ),
    )

    @model_validator(mode="after")
    def validate_action_shape(self) -> DeviceCommand:
        if (
            self.action
            in {"get_port_status", "diagnose_port", "set_port_power"}
            and self.port is None
        ):
            raise ValueError(f"{self.action} requires port")
        if self.action == "set_port_power" and self.enabled is None:
            raise ValueError("set_port_power requires enabled")
        return self


class ConversationTurn(BaseModel):
    request: str
    summary: str
    action: str
    port: int | None = None
    intent: str = ""
    evidence: list[str] = Field(default_factory=list)
    knowledge_titles: list[str] = Field(default_factory=list)
    knowledge_origin: str = ""
    trace: list[str] = Field(default_factory=list)


class DeviceConversationContext(BaseModel):
    """Small, credential-free context used to resolve follow-up requests."""

    last_port: int | None = None
    charging_ports: list[int] = Field(default_factory=list)
    recent_turns: list[ConversationTurn] = Field(default_factory=list)


def build_context_prompt(context: DeviceAgentContext) -> str:
    ports = ", ".join(str(port) for port in context.allowed_ports)
    return "\n".join(
        [
            "你正在为一个设备控制 Agent 生成下一步动作。",
            f"device_id: {context.device_id}",
            f"device_online: {context.online}",
            f"allowed_ports: {ports}",
            f"operator_role: {context.operator_role}",
            f"safety_policy: {context.safety_policy}",
            "身份: DeviceOps，一名设备运维 Agent。",
            (
                "能力: 普通业务交流、设备状态查询、端口诊断与控制、"
                "固件知识问答、结合实时证据和知识库给出建议。"
            ),
            "要求:",
            "- 必须输出结构化动作；普通交流写入 reply 字段。",
            "- 问候、身份、能力、致谢使用 respond_chat。",
            "- 固件、协议、OTA、MQTT、功率或温控原理使用 answer_knowledge。",
            "- 追问上一次结论、依据或执行内容使用 explain_previous。",
            "- 与设备运维和固件知识无关的请求使用 unsupported_request。",
            "- 缺少执行设备任务所需参数时才使用 ask_clarification。",
            "- 只能使用 allowed_ports 里面存在的端口。",
            "- 控制类动作使用 set_port_power，并把 need_confirmation 设为 true。",
            "- 排查端口不充电或异常时使用 diagnose_port。",
            "- 排查整台设备是否异常且没有指定端口时使用 diagnose_device。",
            "- 信息不足时使用 ask_clarification。",
            "- response_focus 表示用户真正要求查看的结果范围。",
            "- 用户只问几号端口充电时使用 charging_ports。",
            "- 用户只问哪些端口连接或在线时使用 connected_ports。",
            "- 用户要求简洁或只回答结果时 response_detail 使用 concise。",
            (
                "- reply 不得声称已经执行工具；设备状态和知识答案必须"
                "等待对应节点返回真实结果。"
            ),
        ]
    )


def build_conversation_prompt(
    conversation: DeviceConversationContext,
) -> str:
    lines = [
        "当前会话上下文:",
        f"- last_port: {conversation.last_port}",
        (
            "- charging_ports: "
            + (
                ", ".join(str(port) for port in conversation.charging_ports)
                if conversation.charging_ports
                else "none"
            )
        ),
    ]
    if conversation.recent_turns:
        lines.append("- recent_turns:")
        lines.extend(
            (
                f"  - 用户: {turn.request}\n"
                f"    动作: {turn.action}\n"
                f"    助手: {turn.summary}"
                + (
                    f"\n    依据: {'；'.join(turn.evidence[:3])}"
                    if turn.evidence
                    else ""
                )
                + (
                    "\n    知识来源: "
                    + "、".join(turn.knowledge_titles[:3])
                    if turn.knowledge_titles
                    else ""
                )
                + (
                    f"\n    回答来源类型: {turn.knowledge_origin}"
                    if turn.knowledge_origin
                    else ""
                )
            )
            for turn in conversation.recent_turns[-4:]
        )
    lines.extend(
        [
            "指代规则:",
            "- 用户说“它、这个端口、刚才那个”时，优先使用 last_port。",
            "- last_port 为空或候选端口不唯一时，必须 ask_clarification。",
        ]
    )
    return "\n".join(lines)


def validate_command_against_context(
    command: DeviceCommand,
    context: DeviceAgentContext,
) -> None:
    if command.device_id != context.device_id:
        raise ValueError(
            f"device_id {command.device_id} does not match current device {context.device_id}"
        )

    device_actions = {
        "get_status",
        "get_port_status",
        "diagnose_device",
        "diagnose_port",
        "set_port_power",
    }
    if not context.online and command.action in device_actions:
        raise ValueError(f"device {context.device_id} is offline")

    if command.port is not None and command.port not in context.allowed_ports:
        ports = ", ".join(str(port) for port in context.allowed_ports)
        raise ValueError(f"port {command.port} is not available; allowed ports: {ports}")

    if command.action == "set_port_power" and not command.need_confirmation:
        raise ValueError("set_port_power must require confirmation")

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

    intent: Literal["query_device", "control_device", "clarify"] = Field(
        description="High-level user intent."
    )
    action: Literal[
        "get_status",
        "get_port_status",
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
            "要求:",
            "- 必须输出结构化动作，不要只输出普通聊天文本。",
            "- 只能使用 allowed_ports 里面存在的端口。",
            "- 控制类动作使用 set_port_power，并把 need_confirmation 设为 true。",
            "- 排查端口不充电或异常时使用 diagnose_port。",
            "- 信息不足时使用 ask_clarification。",
            "- response_focus 表示用户真正要求查看的结果范围。",
            "- 用户只问几号端口充电时使用 charging_ports。",
            "- 用户只问哪些端口连接或在线时使用 connected_ports。",
            "- 用户要求简洁或只回答结果时 response_detail 使用 concise。",
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
            f"  - 用户: {turn.request}\n    助手: {turn.summary}"
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

    if not context.online and command.action != "ask_clarification":
        raise ValueError(f"device {context.device_id} is offline")

    if command.port is not None and command.port not in context.allowed_ports:
        ports = ", ".join(str(port) for port in context.allowed_ports)
        raise ValueError(f"port {command.port} is not available; allowed ports: {ports}")

    if command.action == "set_port_power" and not command.need_confirmation:
        raise ValueError("set_port_power must require confirmation")

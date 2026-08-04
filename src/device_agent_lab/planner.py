from __future__ import annotations

import re
from typing import Protocol

from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    DeviceConversationContext,
    build_conversation_prompt,
    build_context_prompt,
)


class CommandPlanner(Protocol):
    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> DeviceCommand: ...


class RuleBasedCommandPlanner:
    """Offline planner that keeps the application runnable without an API key."""

    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> DeviceCommand:
        response_preferences = _extract_response_preferences(request)
        port = _extract_port(request)
        if (
            port is None
            and conversation is not None
            and _uses_port_reference(request)
        ):
            port = conversation.last_port
        enabled = _extract_power_intent(request)

        if enabled is not None:
            if port is None:
                return DeviceCommand(
                    intent="clarify",
                    action="ask_clarification",
                    device_id=context.device_id,
                    need_confirmation=False,
                    reason="请说明要控制哪个端口。",
                    **response_preferences,
                )
            return DeviceCommand(
                intent="control_device",
                action="set_port_power",
                device_id=context.device_id,
                port=port,
                enabled=enabled,
                need_confirmation=True,
                reason=(
                    f"用户请求{'打开' if enabled else '关闭'}"
                    f"{port}号端口。"
                ),
                **response_preferences,
            )

        if port is not None and _has_diagnostic_intent(request):
            return DeviceCommand(
                intent="query_device",
                action="diagnose_port",
                device_id=context.device_id,
                port=port,
                need_confirmation=False,
                reason=f"采集多项设备证据并诊断{port}号端口。",
                response_focus="diagnosis",
                response_detail=response_preferences["response_detail"],
            )

        if port is not None:
            return DeviceCommand(
                intent="query_device",
                action="get_port_status",
                device_id=context.device_id,
                port=port,
                need_confirmation=False,
                reason=f"查询并分析{port}号端口状态。",
                response_focus="port_status",
                response_detail=response_preferences["response_detail"],
            )

        if response_preferences["response_focus"] == "auto":
            response_preferences["response_focus"] = "device_overview"
        return DeviceCommand(
            intent="query_device",
            action="get_status",
            device_id=context.device_id,
            need_confirmation=False,
            reason="查询设备整体状态。",
            **response_preferences,
        )


class GeminiCommandPlanner:
    """LLM planner that produces the same validated command contract."""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str = "gemini-2.5-flash",
    ) -> None:
        self._model = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0,
        )

    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> DeviceCommand:
        system_prompt = build_context_prompt(context)
        if conversation is not None:
            system_prompt = (
                system_prompt
                + "\n\n"
                + build_conversation_prompt(conversation)
            )
        agent = create_agent(
            model=self._model,
            tools=[],
            system_prompt=system_prompt,
            response_format=DeviceCommand,
        )
        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": request}]}
        )
        command = DeviceCommand.model_validate(
            response["structured_response"]
        )
        return apply_explicit_response_preferences(request, command)


def _extract_port(request: str) -> int | None:
    patterns = (
        r"([1-9]\d*)\s*号?\s*(?:端口|口)",
        r"(?:端口|口|[cC])\s*([1-9]\d*)",
    )
    for pattern in patterns:
        match = re.search(pattern, request)
        if match:
            return int(match.group(1))
    return None


def _extract_power_intent(request: str) -> bool | None:
    normalized = request.lower()
    if any(word in normalized for word in ("关闭", "关掉", "断开", "turn off")):
        return False
    if any(word in normalized for word in ("打开", "开启", "启用", "turn on")):
        return True
    return None


def _has_diagnostic_intent(request: str) -> bool:
    normalized = request.lower()
    return any(
        word in normalized
        for word in (
            "为什么",
            "诊断",
            "排查",
            "异常",
            "无法",
            "不能",
            "不充电",
            "not charging",
            "diagnose",
        )
    )


def _uses_port_reference(request: str) -> bool:
    normalized = request.lower()
    return any(
        phrase in normalized
        for phrase in (
            "它",
            "这个端口",
            "那个端口",
            "刚才那个",
            "刚才的",
            "上面的端口",
            "该端口",
            "the port",
            "that port",
        )
    ) or re.search(r"\bit\b", normalized) is not None


def apply_explicit_response_preferences(
    request: str,
    command: DeviceCommand,
) -> DeviceCommand:
    """Enforce response constraints stated explicitly by the user."""
    preferences = _extract_response_preferences(request)
    updates: dict[str, str] = {}
    if preferences["response_focus"] != "auto":
        updates["response_focus"] = preferences["response_focus"]
    if preferences["response_detail"] != "standard":
        updates["response_detail"] = preferences["response_detail"]
    if not updates:
        return command
    return command.model_copy(update=updates)


def _extract_response_preferences(request: str) -> dict[str, str]:
    normalized = request.lower()
    asks_port_selection = any(
        phrase in normalized
        for phrase in (
            "几号",
            "哪些",
            "哪几个",
            "哪个端口",
            "几个端口",
            "多少个端口",
            "which port",
            "which ports",
            "how many ports",
        )
    )
    mentions_port = any(
        phrase in normalized
        for phrase in ("端口", "几号口", "哪些口", "port")
    )
    if mentions_port and any(
        phrase in normalized
        for phrase in ("充电", "charging")
    ):
        response_focus = "charging_ports"
    elif mentions_port and any(
        phrase in normalized
        for phrase in (
            "在线",
            "连接",
            "接入",
            "插着",
            "connected",
            "online",
        )
    ):
        response_focus = "connected_ports"
    elif any(
        phrase in normalized
        for phrase in ("整体", "全部状态", "设备状态", "概况", "overview")
    ):
        response_focus = "device_overview"
    else:
        response_focus = "auto"

    if any(
        phrase in normalized
        for phrase in (
            "详细",
            "完整",
            "所有参数",
            "具体参数",
            "detail",
        )
    ):
        response_detail = "detailed"
    elif asks_port_selection or any(
        phrase in normalized
        for phrase in (
            "只回答",
            "只告诉",
            "只要",
            "简洁",
            "多余内容",
            "直接说",
            "concise",
            "only answer",
        )
    ):
        response_detail = "concise"
    else:
        response_detail = "standard"

    return {
        "response_focus": response_focus,
        "response_detail": response_detail,
    }

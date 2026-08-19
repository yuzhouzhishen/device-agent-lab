from __future__ import annotations

import json
import re
from typing import Protocol

import httpx
from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    DeviceConversationContext,
    build_conversation_prompt,
    build_context_prompt,
)
from device_agent_lab.metrics import TokenUsage


class PlanResult(BaseModel):
    """A planned command plus what producing it consumed."""

    command: DeviceCommand
    usage: TokenUsage | None = None
    model: str | None = None
    degraded: bool = False
    warning: str | None = None


class CommandPlanner(Protocol):
    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> PlanResult: ...


class FastPathCommandPlanner:
    """Answer unambiguous business small talk without paying model latency."""

    def __init__(self, planner: CommandPlanner) -> None:
        self._planner = planner
        self._rules = RuleBasedCommandPlanner()

    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> PlanResult:
        if _is_conversation_request(request):
            result = await self._rules.plan(
                request,
                context,
                conversation,
            )
            return result.model_copy(update={"model": "rules-fast-path"})
        return await self._planner.plan(request, context, conversation)

    async def aclose(self) -> None:
        close = getattr(self._planner, "aclose", None)
        if close is not None:
            await close()


class RuleBasedCommandPlanner:
    """Offline planner that keeps the application runnable without an API key."""

    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> PlanResult:
        # Keyword rules call no model, so there is no usage to report.
        return PlanResult(
            command=await self._plan_command(request, context, conversation),
        )

    async def _plan_command(
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
        if (
            port is None
            and conversation is not None
            and conversation.last_port is not None
            and _is_diagnostic_follow_up(request, conversation)
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

        if _is_explanation_request(request):
            if conversation is None or not conversation.recent_turns:
                return DeviceCommand(
                    intent="clarify",
                    action="ask_clarification",
                    device_id=context.device_id,
                    need_confirmation=False,
                    reason="当前会话里还没有可以解释的上一条结果。",
                    **response_preferences,
                )
            return DeviceCommand(
                intent="chat",
                action="explain_previous",
                device_id=context.device_id,
                need_confirmation=False,
                reason="解释当前会话中的上一条执行结果及依据。",
                **response_preferences,
            )

        if _is_conversation_request(request):
            return DeviceCommand(
                intent="chat",
                action="respond_chat",
                device_id=context.device_id,
                need_confirmation=False,
                reason="回应业务对话。",
                reply=_conversation_reply(request),
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

        if _is_knowledge_request(request):
            return DeviceCommand(
                intent="knowledge_query",
                action="answer_knowledge",
                device_id=context.device_id,
                need_confirmation=False,
                reason="查询固件与充电协议知识。",
                **response_preferences,
            )

        if _has_diagnostic_intent(request):
            return DeviceCommand(
                intent="query_device",
                action="diagnose_device",
                device_id=context.device_id,
                need_confirmation=False,
                reason="采集设备、端口、PD 和温控证据并检查整体异常。",
                response_focus="diagnosis",
                response_detail=response_preferences["response_detail"],
            )

        if not _is_live_device_request(request, response_preferences):
            return DeviceCommand(
                intent="unsupported",
                action="unsupported_request",
                device_id=context.device_id,
                need_confirmation=False,
                reason="该请求不属于设备运维或固件知识范围。",
                reply=_unsupported_reply(),
                **response_preferences,
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
        self._model_name = model_name
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
    ) -> PlanResult:
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
        structured = DeviceCommand.model_validate(
            response["structured_response"]
        )
        command = DeviceCommand.model_validate(
            _normalize_model_payload(
                request,
                structured.model_dump(),
                context,
                conversation,
            )
        )
        return PlanResult(
            command=apply_explicit_response_preferences(request, command),
            usage=TokenUsage.from_messages(response.get("messages")),
            model=self._model_name,
        )

    async def aclose(self) -> None:
        close = getattr(self._model, "aclose", None)
        if close is not None:
            await close()


class OllamaCommandPlanner:
    """Use Ollama JSON-schema output for local structured planning."""

    def __init__(
        self,
        *,
        model_name: str = "llama3.1:8b",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model_name = model_name
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> PlanResult:
        system_prompt = build_context_prompt(context)
        if conversation is not None:
            system_prompt += "\n\n" + build_conversation_prompt(
                conversation
            )
        response = await self._client.post(
            "/api/chat",
            json={
                "model": self._model_name,
                "stream": False,
                "format": DeviceCommand.model_json_schema(),
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": request},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("message", {}).get("content", "")
        if isinstance(content, str):
            raw_command = json.loads(content)
        else:
            raw_command = content
        command = DeviceCommand.model_validate(
            _normalize_model_payload(
                request,
                raw_command,
                context,
                conversation,
            )
        )
        input_tokens = int(payload.get("prompt_eval_count", 0) or 0)
        output_tokens = int(payload.get("eval_count", 0) or 0)
        return PlanResult(
            command=apply_explicit_response_preferences(request, command),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            model=self._model_name,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class FallbackCommandPlanner:
    """Fall back to deterministic planning when a model is unavailable."""

    def __init__(
        self,
        primary: CommandPlanner,
        fallback: CommandPlanner | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or RuleBasedCommandPlanner()

    async def plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> PlanResult:
        try:
            return await self._primary.plan(
                request,
                context,
                conversation,
            )
        except Exception as exc:  # noqa: BLE001
            result = await self._fallback.plan(
                request,
                context,
                conversation,
            )
            return result.model_copy(
                update={
                    "model": "rules-fallback",
                    "degraded": True,
                    "warning": (
                        "模型规划不可用，已切换为规则规划："
                        + type(exc).__name__
                    ),
                }
            )

    async def aclose(self) -> None:
        for planner in (self._primary, self._fallback):
            close = getattr(planner, "aclose", None)
            if close is not None:
                await close()


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


def _normalize_model_payload(
    request: str,
    payload: object,
    context: DeviceAgentContext,
    conversation: DeviceConversationContext | None,
) -> dict[str, object]:
    """Apply deterministic domain and safety invariants to model JSON."""
    if not isinstance(payload, dict):
        raise ValueError("model command must be a JSON object")
    normalized: dict[str, object] = dict(payload)
    normalized["device_id"] = context.device_id
    port = _extract_port(request)
    if (
        port is None
        and conversation is not None
        and (
            _uses_port_reference(request)
            or _is_diagnostic_follow_up(request, conversation)
        )
    ):
        port = conversation.last_port
    enabled = _extract_power_intent(request)

    if enabled is not None:
        if port is None:
            normalized.update(
                {
                    "intent": "clarify",
                    "action": "ask_clarification",
                    "port": None,
                    "enabled": None,
                    "need_confirmation": False,
                    "reason": "请说明要控制哪个端口。",
                }
            )
        else:
            normalized.update(
                {
                    "intent": "control_device",
                    "action": "set_port_power",
                    "port": port,
                    "enabled": enabled,
                    "need_confirmation": True,
                    "reason": (
                        f"用户请求{'打开' if enabled else '关闭'}"
                        f"{port}号端口。"
                    ),
                }
            )
    elif _is_explanation_request(request):
        if conversation is None or not conversation.recent_turns:
            normalized.update(
                {
                    "intent": "clarify",
                    "action": "ask_clarification",
                    "port": None,
                    "enabled": None,
                    "need_confirmation": False,
                    "reason": (
                        "当前会话里还没有可以解释的上一条结果。"
                    ),
                    "reply": None,
                }
            )
        else:
            normalized.update(
                {
                    "intent": "chat",
                    "action": "explain_previous",
                    "port": None,
                    "enabled": None,
                    "need_confirmation": False,
                    "reason": "解释当前会话中的上一条执行结果及依据。",
                    "reply": None,
                }
            )
    elif _is_conversation_request(request):
        normalized.update(
            {
                "intent": "chat",
                "action": "respond_chat",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
                "reason": "回应业务对话。",
                "reply": _conversation_reply(request),
            }
        )
    elif port is not None and _has_diagnostic_intent(request):
        normalized.update(
            {
                "intent": "query_device",
                "action": "diagnose_port",
                "port": port,
                "enabled": None,
                "need_confirmation": False,
                "reason": f"采集多项设备证据并诊断{port}号端口。",
            }
        )
    elif _is_knowledge_request(request):
        normalized.update(
            {
                "intent": "knowledge_query",
                "action": "answer_knowledge",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
                "reason": "查询固件与充电协议知识。",
                "reply": None,
            }
        )
    elif _has_diagnostic_intent(request):
        normalized.update(
            {
                "intent": "query_device",
                "action": "diagnose_device",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
                "reason": "采集设备、端口、PD 和温控证据并检查整体异常。",
            }
        )
    elif port is not None:
        normalized.update(
            {
                "intent": "query_device",
                "action": "get_port_status",
                "port": port,
                "enabled": None,
                "need_confirmation": False,
                "reason": f"查询并分析{port}号端口状态。",
            }
        )
    elif (
        normalized.get("action")
        in {
            "get_status",
            "get_port_status",
            "diagnose_device",
            "diagnose_port",
        }
        and not _is_live_device_request(
            request,
            _extract_response_preferences(request),
        )
    ):
        normalized.update(
            {
                "intent": "unsupported",
                "action": "unsupported_request",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
                "reason": "该请求不属于设备运维或固件知识范围。",
                "reply": _unsupported_reply(),
            }
        )
    elif (
        normalized.get("action") == "ask_clarification"
        and not _is_live_device_request(
            request,
            _extract_response_preferences(request),
        )
    ):
        normalized.update(
            {
                "intent": "unsupported",
                "action": "unsupported_request",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
                "reason": "该请求不属于设备运维或固件知识范围。",
                "reply": _unsupported_reply(),
            }
        )
    elif normalized.get("action") == "get_port_status":
        normalized.update(
            {
                "intent": "query_device",
                "action": "get_status",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
            }
        )
    elif normalized.get("action") == "diagnose_port":
        normalized.update(
            {
                "intent": "query_device",
                "action": "diagnose_device",
                "port": None,
                "enabled": None,
                "need_confirmation": False,
            }
        )
    elif normalized.get("action") in {
        "respond_chat",
        "answer_knowledge",
        "explain_previous",
        "unsupported_request",
        "ask_clarification",
    }:
        normalized.update(
            {
                "port": None,
                "enabled": None,
                "need_confirmation": False,
            }
        )
        if normalized["action"] == "respond_chat":
            normalized["intent"] = "chat"
            normalized["reason"] = "回应业务对话。"
            normalized["reply"] = (
                normalized.get("reply")
                or _conversation_reply(request)
            )
        elif normalized["action"] == "answer_knowledge":
            normalized["intent"] = "knowledge_query"
            normalized["reason"] = "查询固件与充电协议知识。"
            normalized["reply"] = None
        elif normalized["action"] == "explain_previous":
            normalized["intent"] = "chat"
            normalized["reason"] = "解释当前会话中的上一条执行结果及依据。"
            normalized["reply"] = None
        elif normalized["action"] == "unsupported_request":
            normalized["intent"] = "unsupported"
            normalized["reply"] = (
                normalized.get("reply") or _unsupported_reply()
            )
        else:
            normalized["intent"] = "clarify"
            normalized["reply"] = None
    elif normalized.get("action") != "set_port_power":
        normalized["need_confirmation"] = False

    normalized.setdefault("reason", "根据用户请求生成设备动作。")
    return normalized


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
            "怎么处理",
            "怎么办",
            "如何处理",
            "建议",
            "结合文档",
            "温控模式",
            "温度策略",
        )
    )


def _is_conversation_request(request: str) -> bool:
    normalized = request.strip().lower()
    chinese_or_phrase_match = any(
        phrase in normalized
        for phrase in (
            "你好",
            "您好",
            "嗨",
            "你是谁",
            "你叫什么",
            "介绍一下自己",
            "你能做什么",
            "你会什么",
            "有哪些能力",
            "怎么用",
            "帮助",
            "谢谢",
            "感谢",
            "辛苦了",
            "再见",
        )
    )
    return chinese_or_phrase_match or re.search(
        r"\b(?:hello|hi)\b",
        normalized,
    ) is not None


def _conversation_reply(request: str) -> str:
    normalized = request.strip().lower()
    if any(
        phrase in normalized
        for phrase in ("你是谁", "你叫什么", "介绍一下自己")
    ):
        return (
            "我是 DeviceOps，一名面向充电设备的运维 Agent。"
            "我可以查询和诊断真实设备、控制端口，并结合固件知识库解释问题。"
        )
    if any(
        phrase in normalized
        for phrase in ("你能做什么", "你会什么", "有哪些能力", "怎么用", "帮助")
    ):
        return (
            "我可以查询整机或端口状态、诊断充电与 PD 协商问题、"
            "打开或关闭指定端口，并回答固件、协议、OTA、MQTT、"
            "功率分配和温控相关问题。"
        )
    if any(phrase in normalized for phrase in ("谢谢", "感谢", "辛苦了")):
        return "不客气。你可以继续查询设备、排查端口或询问固件知识。"
    if "再见" in normalized:
        return "再见。设备运维会话会保留最近的业务上下文。"
    return (
        "你好，我是 DeviceOps。你可以让我查询、诊断或控制设备，"
        "也可以直接询问固件和充电协议知识。"
    )


def _unsupported_reply() -> str:
    return (
        "这个请求超出了当前设备运维范围。"
        "我可以处理设备状态、端口控制、充电诊断以及固件和协议知识问题。"
    )


def _is_explanation_request(request: str) -> bool:
    normalized = request.lower()
    compact = normalized.strip(" ？?。！!")
    if compact in {
        "为什么",
        "为什么呢",
        "怎么判断的",
        "怎么得出的",
        "依据呢",
    }:
        return True
    return any(
        phrase in normalized
        for phrase in (
            "刚才为什么",
            "刚才怎么",
            "为什么这么判断",
            "为什么这样判断",
            "依据是什么",
            "上一个结果",
            "上一条结果",
            "解释一下刚才",
            "explain the last",
        )
    )


def _is_knowledge_request(request: str) -> bool:
    normalized = request.lower()
    if _has_live_device_cue(normalized):
        return False
    domain_terms = (
        "固件",
        "协议",
        "pd ",
        "pd2",
        "pd 2",
        "pd3",
        "pd 3",
        "pps",
        "mqtt",
        "ota",
        "wifi",
        "wi-fi",
        "蓝牙",
        "功率分配",
        "温控策略",
        "状态机",
        "启动流程",
        "升级流程",
        "回滚",
        "bootloader",
        "fpga",
        "sw356",
    )
    return any(term in normalized for term in domain_terms)


def _has_live_device_cue(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "当前",
            "现在",
            "实时",
            "几号端口",
            "哪个端口",
            "设备状态",
            "设备怎么样",
            "设备如何",
            "设备情况",
            "在线吗",
            "正在充电",
            "多少瓦",
            "电压",
            "电流",
        )
    ) or _extract_port(normalized) is not None


def _is_live_device_request(
    request: str,
    response_preferences: dict[str, str],
) -> bool:
    normalized = request.lower()
    if response_preferences["response_focus"] != "auto":
        return True
    return _has_live_device_cue(normalized) or any(
        phrase in normalized
        for phrase in (
            "查询设备",
            "检查设备",
            "整机",
            "设备有没有异常",
            "设备是否异常",
            "充电状态",
        )
    )


def _is_diagnostic_follow_up(
    request: str,
    conversation: DeviceConversationContext,
) -> bool:
    if not _has_diagnostic_intent(request):
        return False
    return any(
        turn.action in {"diagnose_port", "diagnose_device"}
        for turn in conversation.recent_turns[-2:]
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
    updates: dict[str, str] = {
        "response_detail": preferences["response_detail"],
    }
    if preferences["response_focus"] != "auto":
        updates["response_focus"] = preferences["response_focus"]
    elif (
        command.action in {"get_status", "diagnose_device"}
        and command.response_focus == "auto"
    ):
        updates["response_focus"] = "device_overview"
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
            "结合文档",
            "深入分析",
            "详细分析",
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

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.audit import JsonlAuditLog
from device_agent_lab.conversation_store import SqliteConversationStore
from device_agent_lab.device_gateway import (
    DeviceGateway,
    McpDeviceGateway,
    MockDeviceGateway,
)
from device_agent_lab.device_ops_service import DeviceOpsService
from device_agent_lab.mcp_client_config import (
    build_device_mcp_connection,
    build_remote_mcp_connection,
)
from device_agent_lab.metrics import TokenPricing
from device_agent_lab.knowledge_gateway import (
    GeneralKnowledgeProvider,
    HttpKnowledgeGateway,
    KnowledgeGateway,
    LocalKnowledgeGateway,
    OllamaGeneralKnowledgeProvider,
)
from device_agent_lab.planner import (
    CommandPlanner,
    FastPathCommandPlanner,
    FallbackCommandPlanner,
    GeminiCommandPlanner,
    OllamaCommandPlanner,
    RuleBasedCommandPlanner,
)


@dataclass(frozen=True)
class RuntimeSettings:
    backend: Literal["mock", "local_mcp", "xdp"] = "mock"
    planner_mode: Literal["auto", "rules", "gemini", "ollama"] = "auto"
    control_mode: Literal["approval", "automatic"] = "approval"
    device_id: str = "DEMO-CP02-001"
    allowed_ports: tuple[int, ...] = (1, 2, 3)
    operator_role: str = "device-operator"
    gemini_model: str = "gemini-2.5-flash"
    ollama_model: str = "llama3.1:8b"
    ollama_url: str = "http://127.0.0.1:11434"
    knowledge_url: str | None = None
    knowledge_timeout_seconds: float = 120.0
    general_knowledge_fallback: bool = True
    general_knowledge_timeout_seconds: float = 45.0
    control_verify_delay_seconds: float = 0.75
    control_verify_attempts: int = 4
    audit_path: Path = Path("var/audit.jsonl")
    session_path: Path = Path("var/sessions.sqlite3")
    planner_fallback: bool = True
    conversation_namespace: str = ""
    pricing: TokenPricing | None = None
    gemini_api_key: str | None = field(default=None, repr=False)
    xdp_mcp_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str] | None = None,
    ) -> RuntimeSettings:
        env = os.environ if values is None else values
        backend = env.get("DEVICE_BACKEND", "mock").strip().lower()
        if backend not in {"mock", "local_mcp", "xdp"}:
            raise ValueError(
                "DEVICE_BACKEND must be mock, local_mcp, or xdp"
            )
        planner_mode = env.get("DEVICE_PLANNER", "auto").strip().lower()
        if planner_mode not in {"auto", "rules", "gemini", "ollama"}:
            raise ValueError(
                "DEVICE_PLANNER must be auto, rules, gemini, or ollama"
            )
        control_mode = env.get(
            "DEVICE_CONTROL_MODE",
            "approval",
        ).strip().lower()
        if control_mode not in {"approval", "automatic"}:
            raise ValueError(
                "DEVICE_CONTROL_MODE must be approval or automatic"
            )
        allowed_ports = _parse_ports(
            env.get("DEVICE_ALLOWED_PORTS", "1,2,3")
        )
        xdp_mcp_url = env.get("XDP_MCP_URL") or None
        if backend == "xdp" and not xdp_mcp_url:
            raise ValueError("XDP_MCP_URL is required for DEVICE_BACKEND=xdp")
        audit_path = Path(
            env.get("DEVICE_AUDIT_PATH", "var/audit.jsonl")
        )
        if not audit_path.is_absolute():
            audit_path = (
                Path(__file__).resolve().parents[2] / audit_path
            )
        session_path = Path(
            env.get("DEVICE_SESSION_DB", "var/sessions.sqlite3")
        )
        if not session_path.is_absolute():
            session_path = (
                Path(__file__).resolve().parents[2] / session_path
            )
        try:
            knowledge_timeout_seconds = float(
                env.get("FIRMWARE_RAG_TIMEOUT_SECONDS", "120")
            )
            general_knowledge_timeout_seconds = float(
                env.get("GENERAL_KNOWLEDGE_TIMEOUT_SECONDS", "45")
            )
        except ValueError as exc:
            raise ValueError(
                "knowledge timeout settings must be numbers"
            ) from exc
        if knowledge_timeout_seconds <= 0:
            raise ValueError(
                "FIRMWARE_RAG_TIMEOUT_SECONDS must be positive"
            )
        if general_knowledge_timeout_seconds <= 0:
            raise ValueError(
                "GENERAL_KNOWLEDGE_TIMEOUT_SECONDS must be positive"
            )
        try:
            control_verify_delay_seconds = float(
                env.get(
                    "DEVICE_CONTROL_VERIFY_DELAY_SECONDS",
                    "0.75",
                )
            )
            control_verify_attempts = int(
                env.get("DEVICE_CONTROL_VERIFY_ATTEMPTS", "4")
            )
        except ValueError as exc:
            raise ValueError(
                "device control verification settings must be numbers"
            ) from exc
        if control_verify_delay_seconds < 0:
            raise ValueError(
                "DEVICE_CONTROL_VERIFY_DELAY_SECONDS must not be negative"
            )
        if control_verify_attempts <= 0:
            raise ValueError(
                "DEVICE_CONTROL_VERIFY_ATTEMPTS must be positive"
            )

        return cls(
            backend=backend,
            planner_mode=planner_mode,
            control_mode=control_mode,
            device_id=env.get("DEVICE_ID", "DEMO-CP02-001"),
            allowed_ports=allowed_ports,
            operator_role=env.get("DEVICE_OPERATOR_ROLE", "device-operator"),
            gemini_api_key=env.get("GEMINI_API_KEY") or None,
            gemini_model=env.get("GEMINI_MODEL", "gemini-2.5-flash"),
            ollama_model=env.get("OLLAMA_MODEL", "llama3.1:8b"),
            ollama_url=env.get(
                "OLLAMA_BASE_URL",
                "http://127.0.0.1:11434",
            ),
            knowledge_url=env.get("FIRMWARE_RAG_URL") or None,
            knowledge_timeout_seconds=knowledge_timeout_seconds,
            general_knowledge_fallback=_parse_bool(
                env.get("GENERAL_KNOWLEDGE_FALLBACK", "true"),
                name="GENERAL_KNOWLEDGE_FALLBACK",
            ),
            general_knowledge_timeout_seconds=(
                general_knowledge_timeout_seconds
            ),
            control_verify_delay_seconds=control_verify_delay_seconds,
            control_verify_attempts=control_verify_attempts,
            xdp_mcp_url=xdp_mcp_url,
            audit_path=audit_path,
            session_path=session_path,
            planner_fallback=_parse_bool(
                env.get("DEVICE_PLANNER_FALLBACK", "true"),
                name="DEVICE_PLANNER_FALLBACK",
            ),
            pricing=_parse_pricing(env),
        )


@dataclass
class DeviceOpsRuntime:
    settings: RuntimeSettings
    context: DeviceAgentContext
    gateway: DeviceGateway
    planner: CommandPlanner
    knowledge_gateway: KnowledgeGateway
    general_knowledge_provider: GeneralKnowledgeProvider | None
    service: DeviceOpsService

    async def aclose(self) -> None:
        for resource in (
            self.gateway,
            self.planner,
            self.knowledge_gateway,
            self.general_knowledge_provider,
        ):
            close = getattr(resource, "aclose", None)
            if close is not None:
                await close()


async def create_runtime(settings: RuntimeSettings) -> DeviceOpsRuntime:
    gateway = await _create_gateway(settings)
    planner = _create_planner(settings)
    knowledge_gateway = _create_knowledge_gateway(settings)
    general_knowledge_provider = _create_general_knowledge_provider(
        settings
    )
    context = DeviceAgentContext(
        device_id=settings.device_id,
        online=True,
        allowed_ports=list(settings.allowed_ports),
        operator_role=settings.operator_role,
    )
    service = DeviceOpsService(
        planner=planner,
        gateway=gateway,
        knowledge_gateway=knowledge_gateway,
        general_knowledge_provider=general_knowledge_provider,
        audit_log=JsonlAuditLog(settings.audit_path),
        conversation_store=SqliteConversationStore(
            settings.session_path
        ),
        conversation_namespace=settings.conversation_namespace,
        pricing=settings.pricing,
        control_verify_delay_seconds=(
            settings.control_verify_delay_seconds
        ),
        control_verify_attempts=settings.control_verify_attempts,
    )
    return DeviceOpsRuntime(
        settings=settings,
        context=context,
        gateway=gateway,
        planner=planner,
        knowledge_gateway=knowledge_gateway,
        general_knowledge_provider=general_knowledge_provider,
        service=service,
    )


async def _create_gateway(settings: RuntimeSettings) -> DeviceGateway:
    if settings.backend == "mock":
        return MockDeviceGateway()
    if settings.backend == "local_mcp":
        return await McpDeviceGateway.connect(
            build_device_mcp_connection(),
            profile="lab",
            device_id=settings.device_id,
        )
    if settings.xdp_mcp_url is None:
        raise ValueError("XDP_MCP_URL is required for xdp backend")
    return await McpDeviceGateway.connect(
        build_remote_mcp_connection(settings.xdp_mcp_url),
        profile="xdp",
        device_id=settings.device_id,
    )


def _create_planner(settings: RuntimeSettings) -> CommandPlanner:
    use_gemini = settings.planner_mode == "gemini" or (
        settings.planner_mode == "auto" and settings.gemini_api_key is not None
    )
    if settings.planner_mode == "rules" or (
        settings.planner_mode == "auto" and not use_gemini
    ):
        return RuleBasedCommandPlanner()
    if settings.planner_mode == "ollama":
        primary: CommandPlanner = OllamaCommandPlanner(
            model_name=settings.ollama_model,
            base_url=settings.ollama_url,
        )
    else:
        if settings.gemini_api_key is None:
            raise ValueError(
                "GEMINI_API_KEY is required for DEVICE_PLANNER=gemini"
            )
        primary = GeminiCommandPlanner(
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
        )
    planner = (
        FallbackCommandPlanner(primary)
        if settings.planner_fallback
        else primary
    )
    return FastPathCommandPlanner(planner)


def _create_knowledge_gateway(
    settings: RuntimeSettings,
) -> KnowledgeGateway:
    if settings.knowledge_url is None:
        return LocalKnowledgeGateway()
    return HttpKnowledgeGateway(
        settings.knowledge_url,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )


def _create_general_knowledge_provider(
    settings: RuntimeSettings,
) -> GeneralKnowledgeProvider | None:
    if (
        not settings.general_knowledge_fallback
        or settings.planner_mode != "ollama"
    ):
        return None
    return OllamaGeneralKnowledgeProvider(
        model_name=settings.ollama_model,
        base_url=settings.ollama_url,
        timeout_seconds=settings.general_knowledge_timeout_seconds,
    )


def _parse_pricing(env: Mapping[str, str]) -> TokenPricing | None:
    """Read operator-supplied token prices; absent prices mean no cost line.

    Vendor rates change often enough that baking them into the code would
    quietly produce wrong numbers, so runs report tokens either way and only
    report money when someone states the current price.
    """
    raw_input = env.get("DEVICE_TOKEN_PRICE_INPUT")
    raw_output = env.get("DEVICE_TOKEN_PRICE_OUTPUT")
    if not raw_input and not raw_output:
        return None
    try:
        input_per_1m = float(raw_input or 0)
        output_per_1m = float(raw_output or 0)
    except ValueError as exc:
        raise ValueError(
            "DEVICE_TOKEN_PRICE_INPUT and DEVICE_TOKEN_PRICE_OUTPUT "
            "must be numbers per 1M tokens"
        ) from exc
    if input_per_1m < 0 or output_per_1m < 0:
        raise ValueError("token prices must not be negative")
    return TokenPricing(
        input_per_1m=input_per_1m,
        output_per_1m=output_per_1m,
        currency=env.get("DEVICE_TOKEN_PRICE_CURRENCY", "CNY"),
    )


def _parse_ports(value: str) -> tuple[int, ...]:
    try:
        ports = tuple(
            sorted({int(item.strip()) for item in value.split(",") if item.strip()})
        )
    except ValueError as exc:
        raise ValueError("DEVICE_ALLOWED_PORTS must be comma-separated integers") from exc
    if not ports or any(port <= 0 for port in ports):
        raise ValueError("DEVICE_ALLOWED_PORTS must contain positive integers")
    return ports


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")

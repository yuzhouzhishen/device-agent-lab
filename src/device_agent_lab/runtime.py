from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.audit import JsonlAuditLog
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
from device_agent_lab.planner import (
    CommandPlanner,
    GeminiCommandPlanner,
    RuleBasedCommandPlanner,
)


@dataclass(frozen=True)
class RuntimeSettings:
    backend: Literal["mock", "local_mcp", "xdp"] = "mock"
    planner_mode: Literal["auto", "rules", "gemini"] = "auto"
    device_id: str = "DEMO-CP02-001"
    allowed_ports: tuple[int, ...] = (1, 2, 3)
    operator_role: str = "device-operator"
    gemini_model: str = "gemini-2.5-flash"
    audit_path: Path = Path("var/audit.jsonl")
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
        if planner_mode not in {"auto", "rules", "gemini"}:
            raise ValueError(
                "DEVICE_PLANNER must be auto, rules, or gemini"
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

        return cls(
            backend=backend,
            planner_mode=planner_mode,
            device_id=env.get("DEVICE_ID", "DEMO-CP02-001"),
            allowed_ports=allowed_ports,
            operator_role=env.get("DEVICE_OPERATOR_ROLE", "device-operator"),
            gemini_api_key=env.get("GEMINI_API_KEY") or None,
            gemini_model=env.get("GEMINI_MODEL", "gemini-2.5-flash"),
            xdp_mcp_url=xdp_mcp_url,
            audit_path=audit_path,
        )


@dataclass
class DeviceOpsRuntime:
    settings: RuntimeSettings
    context: DeviceAgentContext
    gateway: DeviceGateway
    planner: CommandPlanner
    service: DeviceOpsService

    async def aclose(self) -> None:
        close = getattr(self.gateway, "aclose", None)
        if close is not None:
            await close()


async def create_runtime(settings: RuntimeSettings) -> DeviceOpsRuntime:
    gateway = await _create_gateway(settings)
    planner = _create_planner(settings)
    context = DeviceAgentContext(
        device_id=settings.device_id,
        online=True,
        allowed_ports=list(settings.allowed_ports),
        operator_role=settings.operator_role,
    )
    service = DeviceOpsService(
        planner=planner,
        gateway=gateway,
        audit_log=JsonlAuditLog(settings.audit_path),
    )
    return DeviceOpsRuntime(
        settings=settings,
        context=context,
        gateway=gateway,
        planner=planner,
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
    if not use_gemini:
        return RuleBasedCommandPlanner()
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is required for DEVICE_PLANNER=gemini")
    return GeminiCommandPlanner(
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_model,
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

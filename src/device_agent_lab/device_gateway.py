from __future__ import annotations

import json
from contextlib import AsyncExitStack
from collections.abc import Sequence
from typing import Any, Literal, Protocol

from device_agent_lab.mock_device import DeviceController


XDP_READ_ONLY_PROBE_TOOLS = (
    "get_device_info",
    "get_machine_facts",
    "get_port_details",
    "get_charging_status",
    "get_temperature_mode",
)
_PUBLIC_XDP_INFO_FIELDS = (
    "model",
    "app_version",
    "fpga_version",
    "channel",
    "rssi",
    "wifi_protocol",
)
_PUBLIC_MACHINE_FACT_FIELDS = (
    "product_family",
    "brand_en",
    "brand_zh",
    "friendly_name_en",
    "friendly_name_zh",
    "max_power_budget",
    "ports",
)
_PUBLIC_PD_STATUS_FIELDS = (
    "port",
    "battery_present",
    "battery_invalid",
    "has_battery",
    "has_emarker",
    "cable_is_active",
    "cable_max_vbus_voltage",
    "cable_max_vbus_current",
    "operating_current",
    "operating_voltage",
    "pd_revision",
    "pps_charging_supported",
    "request_capability_mismatch",
    "sink_minimum_pdp",
    "sink_operational_pdp",
    "sink_maximum_pdp",
    "status_temperature",
    "device_brand_en",
    "device_brand_zh",
    "device_name_en",
    "device_name_zh",
    "cable_brand_en",
    "cable_brand_zh",
    "cable_name_en",
    "cable_name_zh",
    "error",
)


class DeviceGatewayError(RuntimeError):
    """Raised when a device backend cannot satisfy an operation."""


class DeviceGateway(Protocol):
    """Device operations used by the application workflow."""

    async def get_status(self) -> dict[str, Any]: ...

    async def get_port_status(self, port_id: int) -> dict[str, Any]: ...

    async def get_charging_status(self) -> dict[str, Any]: ...

    async def get_port_pd_status(self) -> dict[str, Any]: ...

    async def get_temperature_mode(self) -> dict[str, Any]: ...

    async def set_port_power(
        self,
        port_id: int,
        *,
        enabled: bool,
        power_w: float | None = None,
    ) -> dict[str, Any]: ...


class MockDeviceGateway:
    """Async adapter for the in-memory device used by local demos and tests."""

    def __init__(self, controller: DeviceController | None = None) -> None:
        self._controller = controller or DeviceController()

    async def get_status(self) -> dict[str, Any]:
        return self._controller.get_status()

    async def get_port_status(self, port_id: int) -> dict[str, Any]:
        return self._controller.get_port_status(port_id)

    async def get_charging_status(self) -> dict[str, Any]:
        return _charging_status_from_ports(
            self._controller.get_status()["ports"]
        )

    async def get_port_pd_status(self) -> dict[str, Any]:
        ports = self._controller.get_status()["ports"]
        return {
            "ports": [
                {
                    "port": int(port_id),
                    "pd_revision": "3.1",
                    "pps_charging_supported": True,
                    "operating_voltage": int(port["voltage_v"] * 1000),
                    "operating_current": int(port["current_a"] * 1000),
                }
                for port_id, port in ports.items()
                if port["mode"] == "charging"
            ]
        }

    async def get_temperature_mode(self) -> dict[str, Any]:
        return {
            "mode": 0,
            "mode_name": "POWER_PRIORITY",
        }

    async def set_port_power(
        self,
        port_id: int,
        *,
        enabled: bool,
        power_w: float | None = None,
    ) -> dict[str, Any]:
        result = self._controller.set_port_power(
            port_id,
            enabled=enabled,
            power_w=power_w,
        )
        return {
            "ok": bool(result.get("ok", False)),
            "message": str(result.get("message", "port updated")),
            "port": self._controller.get_port_status(port_id),
        }


class McpTool(Protocol):
    name: str

    async def ainvoke(self, arguments: dict[str, Any]) -> object: ...


class McpDeviceGateway:
    """Map the application device interface onto MCP tools."""

    def __init__(
        self,
        tools: Sequence[McpTool],
        *,
        profile: Literal["lab", "xdp"],
        device_id: str,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._profile = profile
        self._device_id = device_id
        self._session_stack: AsyncExitStack | None = None

    @classmethod
    def from_tools(
        cls,
        tools: Sequence[McpTool],
        *,
        profile: Literal["lab", "xdp"],
        device_id: str,
    ) -> McpDeviceGateway:
        return cls(tools, profile=profile, device_id=device_id)

    @classmethod
    async def connect(
        cls,
        connection: dict[str, Any],
        *,
        profile: Literal["lab", "xdp"],
        device_id: str,
    ) -> McpDeviceGateway:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools

        client = MultiServerMCPClient({"device": connection})
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(client.session("device"))
            tools = await load_mcp_tools(
                session,
                server_name="device",
                handle_tool_errors=False,
            )
        except Exception:
            await stack.aclose()
            raise
        gateway = cls(tools, profile=profile, device_id=device_id)
        gateway._session_stack = stack
        return gateway

    @property
    def available_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    async def aclose(self) -> None:
        if self._session_stack is not None:
            await self._session_stack.aclose()
            self._session_stack = None

    async def probe_xdp_read_only(self) -> dict[str, Any]:
        """Call a fixed allowlist of XDP query tools without exposing controls."""
        if self._profile != "xdp":
            raise DeviceGatewayError("XDP probe requires the xdp MCP profile")

        checks: dict[str, dict[str, Any]] = {}
        for tool_name in XDP_READ_ONLY_PROBE_TOOLS:
            if tool_name not in self._tools:
                checks[tool_name] = {
                    "status": "missing",
                    "error": "tool is not exposed by the MCP server",
                }
                continue
            try:
                checks[tool_name] = {
                    "status": "ok",
                    "data": await self._invoke_json(tool_name, {}),
                }
            except (DeviceGatewayError, TimeoutError) as exc:
                checks[tool_name] = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }

        return {
            "available_tools": list(self.available_tools),
            "checks": checks,
        }

    async def get_status(self) -> dict[str, Any]:
        if self._profile == "lab":
            return await self._invoke_json("device_get_status", {})

        info: dict[str, Any] = {}
        machine_facts: dict[str, Any] = {}
        if "get_device_info" in self._tools:
            info = await self._invoke_json("get_device_info", {})
        if "get_machine_facts" in self._tools:
            machine_facts = await self._invoke_json(
                "get_machine_facts",
                {},
            )
        details = await self._invoke_json("get_port_details", {})
        ports = {
            str(port["port_id"]): port
            for port in (
                self._normalize_xdp_port(item)
                for item in details.get("ports", [])
            )
        }
        return {
            "device_id": self._device_id,
            **_select_fields(info, _PUBLIC_XDP_INFO_FIELDS),
            "machine": _select_fields(
                machine_facts,
                _PUBLIC_MACHINE_FACT_FIELDS,
            ),
            "online": True,
            "ports": ports,
        }

    async def get_port_status(self, port_id: int) -> dict[str, Any]:
        if self._profile == "lab":
            return await self._invoke_json(
                "device_get_port_status",
                {"params": {"port_id": port_id}},
            )

        details = await self._invoke_json("get_port_details", {})
        for item in details.get("ports", []):
            if int(item.get("port", -1)) == port_id:
                return self._normalize_xdp_port(item)
        raise DeviceGatewayError(f"xdp-mcp did not return port {port_id}")

    async def get_charging_status(self) -> dict[str, Any]:
        if self._profile == "lab":
            status = await self.get_status()
            return _charging_status_from_ports(status.get("ports", {}))
        return await self._invoke_json("get_charging_status", {})

    async def get_port_pd_status(self) -> dict[str, Any]:
        if self._profile == "lab":
            return {"ports": []}
        result = await self._invoke_json("get_port_pd_status", {})
        ports = result.get("ports", [])
        return {
            "ports": [
                _select_fields(item, _PUBLIC_PD_STATUS_FIELDS)
                for item in ports
                if isinstance(item, dict)
            ]
        }

    async def get_temperature_mode(self) -> dict[str, Any]:
        if self._profile == "lab":
            return {
                "mode": 0,
                "mode_name": "POWER_PRIORITY",
            }
        return await self._invoke_json("get_temperature_mode", {})

    async def set_port_power(
        self,
        port_id: int,
        *,
        enabled: bool,
        power_w: float | None = None,
    ) -> dict[str, Any]:
        if self._profile == "lab":
            return await self._invoke_json(
                "device_set_port_power",
                {
                    "params": {
                        "port_id": port_id,
                        "enabled": enabled,
                        "power_w": power_w,
                    }
                },
            )

        if power_w is not None:
            raise DeviceGatewayError(
                "xdp profile cannot set one port's target power through turn_on_port"
            )
        tool_name = "turn_on_port" if enabled else "turn_off_port"
        response = await self._invoke(tool_name, {"ports": [port_id]})
        return {
            "ok": True,
            "action": "set_port_power",
            "message": _stringify_mcp_result(response),
            "requested": {
                "port_id": port_id,
                "enabled": enabled,
            },
        }

    async def _invoke_json(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self._invoke(tool_name, arguments)
        text = _stringify_mcp_result(result)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DeviceGatewayError(
                f"MCP tool {tool_name} returned non-JSON data: {text}"
            ) from exc
        if not isinstance(parsed, dict):
            raise DeviceGatewayError(
                f"MCP tool {tool_name} returned {type(parsed).__name__}, expected object"
            )
        return parsed

    async def _invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> object:
        try:
            tool = self._tools[tool_name]
        except KeyError as exc:
            available = ", ".join(sorted(self._tools))
            raise DeviceGatewayError(
                f"MCP tool {tool_name!r} is unavailable; found: {available}"
            ) from exc
        try:
            return await tool.ainvoke(arguments)
        except Exception as exc:
            raise DeviceGatewayError(f"MCP tool {tool_name} failed: {exc}") from exc

    @staticmethod
    def _normalize_xdp_port(item: dict[str, Any]) -> dict[str, Any]:
        voltage_v = round(float(item.get("vout_mv", 0)) / 1000, 3)
        current_a = round(float(item.get("iout_ma", 0)) / 1000, 3)
        power_w = round(voltage_v * current_a, 2)
        connected = bool(item.get("connected", False))
        if connected and power_w > 0:
            mode = "charging"
        elif connected:
            mode = "standby"
        else:
            mode = "off"
        return {
            "port_id": int(item["port"]),
            "mode": mode,
            "voltage_v": voltage_v,
            "current_a": current_a,
            "power_w": power_w,
            "connected": connected,
            "protocol": item.get("fc_protocol"),
        }


def _stringify_mcp_result(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("text"), str):
            return result["text"]
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, list):
        texts = [
            item["text"]
            for item in result
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    raise DeviceGatewayError(
        f"unsupported MCP tool result type: {type(result).__name__}"
    )


def _select_fields(
    payload: dict[str, Any],
    fields: Sequence[str],
) -> dict[str, Any]:
    return {
        field: payload[field]
        for field in fields
        if field in payload
    }


def _charging_status_from_ports(
    ports: dict[str, Any],
) -> dict[str, Any]:
    active_ports = [
        int(port_id)
        for port_id, port in ports.items()
        if isinstance(port, dict) and port.get("mode") == "charging"
    ]
    return {
        "status_bitmask": sum(
            1 << (port_id - 1)
            for port_id in active_ports
        ),
        "charging_ports": {
            f"port_{port_id}": True
            for port_id in active_ports
        },
    }

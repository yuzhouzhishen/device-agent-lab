from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from device_agent_lab.mock_device import DeviceController


mcp = FastMCP("device_agent_lab_mcp")
controller = DeviceController()


class PortStatusInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    port_id: int = Field(..., description="Device port id, valid values are 1, 2, or 3.", ge=1, le=3)


class SetPortPowerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    port_id: int = Field(..., description="Device port id, valid values are 1, 2, or 3.", ge=1, le=3)
    enabled: bool = Field(..., description="Whether the port should be enabled.")
    power_w: float | None = Field(
        default=None,
        description="Target output power in watts. Required when enabling a port if a non-default power is desired.",
        gt=0,
        le=100,
    )


@mcp.tool(
    name="device_get_status",
    annotations={
        "title": "Get Device Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def device_get_status() -> str:
    """Return the full mock device status as JSON.

    Args:
        None.

    Returns:
        str: JSON string containing device psn, online status, firmware, temperature,
        total power, and per-port state.
    """
    return json.dumps(controller.get_status(), ensure_ascii=False, indent=2)


@mcp.tool(
    name="device_get_port_status",
    annotations={
        "title": "Get Device Port Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def device_get_port_status(params: PortStatusInput) -> str:
    """Return one mock device port's status as JSON.

    Args:
        params (PortStatusInput): Input with port_id between 1 and 3.

    Returns:
        str: JSON string containing mode, voltage, current, power, and connection state.
    """
    try:
        return json.dumps(controller.get_port_status(params.port_id), ensure_ascii=False, indent=2)
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool(
    name="device_set_port_power",
    annotations={
        "title": "Set Device Port Power",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def device_set_port_power(params: SetPortPowerInput) -> str:
    """Enable or disable a mock device port and optionally set target power.

    Args:
        params (SetPortPowerInput): Input containing:
            - port_id (int): Port id between 1 and 3.
            - enabled (bool): Whether the port should be enabled.
            - power_w (float | None): Optional target power in watts.

    Returns:
        str: JSON string with ok/message fields and the updated device snapshot.
    """
    try:
        result = controller.set_port_power(
            port_id=params.port_id,
            enabled=params.enabled,
            power_w=params.power_w,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except ValueError as exc:
        return f"Error: {exc}"


if __name__ == "__main__":
    mcp.run()


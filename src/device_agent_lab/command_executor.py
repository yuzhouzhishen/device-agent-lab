from __future__ import annotations

from typing import Any

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    validate_command_against_context,
)
from device_agent_lab.mock_device import DeviceController


def execute_device_command(
    command: DeviceCommand,
    context: DeviceAgentContext,
    controller: DeviceController,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Validate and execute a structured Agent command against a device."""
    validate_command_against_context(command, context)

    if command.action == "ask_clarification":
        return {
            "ok": False,
            "action": command.action,
            "message": command.reason,
        }

    if command.action == "get_status":
        return {
            "ok": True,
            "action": command.action,
            "device": controller.get_status(),
        }

    if command.action == "get_port_status":
        if command.port is None:
            raise ValueError("get_port_status requires port")
        return {
            "ok": True,
            "action": command.action,
            "port": controller.get_port_status(command.port),
        }

    if command.action == "set_port_power":
        if command.port is None or command.enabled is None:
            raise ValueError("set_port_power requires port and enabled")

        if command.need_confirmation and not confirmed:
            return {
                "ok": False,
                "action": command.action,
                "requires_confirmation": True,
                "message": "control command is waiting for confirmation",
                "command": command.model_dump(),
            }

        return controller.set_port_power(
            command.port,
            enabled=command.enabled,
        )

    raise ValueError(f"unsupported action: {command.action}")

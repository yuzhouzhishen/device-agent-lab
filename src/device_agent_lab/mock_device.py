from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class PortMode(str, Enum):
    OFF = "off"
    CHARGING = "charging"
    STANDBY = "standby"


@dataclass
class PortState:
    port_id: int
    mode: PortMode
    voltage_v: float
    current_a: float
    power_w: float
    connected: bool


@dataclass
class DeviceState:
    psn: str
    online: bool
    temperature_c: float
    total_power_w: float
    firmware: str
    ports: dict[int, PortState]


class DeviceController:
    """In-memory mock device controller for local Agent/MCP demos."""

    def __init__(self, psn: str = "DEMO-CP02-001") -> None:
        self._state = DeviceState(
            psn=psn,
            online=True,
            temperature_c=42.5,
            total_power_w=45.0,
            firmware="demo-1.0.0",
            ports={
                1: PortState(1, PortMode.CHARGING, 20.0, 2.25, 45.0, True),
                2: PortState(2, PortMode.STANDBY, 5.0, 0.0, 0.0, False),
                3: PortState(3, PortMode.OFF, 0.0, 0.0, 0.0, False),
            },
        )

    def get_status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the whole device."""
        data = asdict(self._state)
        data["ports"] = {
            str(port_id): self._format_port(port)
            for port_id, port in self._state.ports.items()
        }
        return data

    def get_port_status(self, port_id: int) -> dict[str, Any]:
        """Return one port's current state."""
        port = self._get_port(port_id)
        return self._format_port(port)

    def set_port_power(self, port_id: int, enabled: bool, power_w: float | None = None) -> dict[str, Any]:
        """Set a port on/off and optionally update target output power."""
        port = self._get_port(port_id)

        if not enabled:
            port.mode = PortMode.OFF
            port.voltage_v = 0.0
            port.current_a = 0.0
            port.power_w = 0.0
            port.connected = False
        else:
            requested_power = 15.0 if power_w is None else power_w
            if requested_power <= 0:
                raise ValueError("power_w must be greater than 0 when enabling a port")
            if requested_power > 100:
                raise ValueError("power_w must not exceed 100W in the demo device")

            port.mode = PortMode.CHARGING
            port.voltage_v = 20.0 if requested_power > 30 else 9.0
            port.power_w = round(requested_power, 2)
            port.current_a = round(port.power_w / port.voltage_v, 2)
            port.connected = True

        self._recalculate_total_power()
        return {
            "ok": True,
            "message": f"port {port_id} updated",
            "device": self.get_status(),
        }

    def _get_port(self, port_id: int) -> PortState:
        try:
            return self._state.ports[port_id]
        except KeyError as exc:
            valid_ports = ", ".join(str(pid) for pid in sorted(self._state.ports))
            raise ValueError(f"unknown port_id {port_id}; valid ports: {valid_ports}") from exc

    def _recalculate_total_power(self) -> None:
        self._state.total_power_w = round(sum(port.power_w for port in self._state.ports.values()), 2)
        self._state.temperature_c = round(36.0 + self._state.total_power_w * 0.14, 1)

    @staticmethod
    def _format_port(port: PortState) -> dict[str, Any]:
        data = asdict(port)
        data["mode"] = port.mode.value
        return data


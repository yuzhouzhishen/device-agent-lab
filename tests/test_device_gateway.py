from __future__ import annotations

import json
import unittest
from typing import Any

from device_agent_lab.device_gateway import McpDeviceGateway, MockDeviceGateway
from device_agent_lab.mock_device import DeviceController


class FakeMcpTool:
    def __init__(self, name: str, result: object) -> None:
        self.name = name
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, arguments: dict[str, Any]) -> object:
        self.calls.append(arguments)
        return self.result


class MockDeviceGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_and_control_share_one_async_interface(self) -> None:
        gateway = MockDeviceGateway(DeviceController())

        before = await gateway.get_port_status(2)
        updated = await gateway.set_port_power(2, enabled=True)
        after = await gateway.get_port_status(2)

        self.assertEqual(before["mode"], "standby")
        self.assertTrue(updated["ok"])
        self.assertEqual(after["mode"], "charging")


class McpDeviceGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_lab_profile_maps_gateway_calls_to_local_mcp_tools(self) -> None:
        status_tool = FakeMcpTool(
            "device_get_port_status",
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "port_id": 2,
                            "mode": "standby",
                            "voltage_v": 5.0,
                            "current_a": 0.0,
                            "power_w": 0.0,
                            "connected": False,
                        }
                    ),
                }
            ],
        )
        control_tool = FakeMcpTool(
            "device_set_port_power",
            [{"type": "text", "text": json.dumps({"ok": True})}],
        )
        gateway = McpDeviceGateway.from_tools(
            [status_tool, control_tool],
            profile="lab",
            device_id="DEMO-CP02-001",
        )

        status = await gateway.get_port_status(2)
        result = await gateway.set_port_power(2, enabled=True)

        self.assertEqual(status["mode"], "standby")
        self.assertTrue(result["ok"])
        self.assertEqual(status_tool.calls, [{"params": {"port_id": 2}}])
        self.assertEqual(
            control_tool.calls,
            [{"params": {"port_id": 2, "enabled": True, "power_w": None}}],
        )

    async def test_xdp_profile_normalizes_remote_port_data_and_control(self) -> None:
        details_tool = FakeMcpTool(
            "get_port_details",
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ports": [
                                {
                                    "port": 2,
                                    "connected": True,
                                    "iout_ma": 2000,
                                    "vout_mv": 9000,
                                    "fc_protocol": "PD",
                                }
                            ]
                        }
                    ),
                }
            ],
        )
        control_tool = FakeMcpTool(
            "turn_off_port",
            [{"type": "text", "text": "ports [2] turned off"}],
        )
        gateway = McpDeviceGateway.from_tools(
            [details_tool, control_tool],
            profile="xdp",
            device_id="DEVICE-FROM-ENV",
        )

        status = await gateway.get_port_status(2)
        result = await gateway.set_port_power(2, enabled=False)

        self.assertEqual(status["port_id"], 2)
        self.assertEqual(status["voltage_v"], 9.0)
        self.assertEqual(status["current_a"], 2.0)
        self.assertEqual(status["power_w"], 18.0)
        self.assertEqual(status["mode"], "charging")
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["requested"],
            {"port_id": 2, "enabled": False},
        )
        self.assertNotIn("port", result)
        self.assertEqual(control_tool.calls, [{"ports": [2]}])

    async def test_xdp_status_excludes_private_device_identifiers(self) -> None:
        info_tool = FakeMcpTool(
            "get_device_info",
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "psn": "private-psn",
                            "ssid": "private-network",
                            "bssid": "00:11:22:33:44:55",
                            "model": "pro",
                            "app_version": "2.92.92",
                        }
                    ),
                }
            ],
        )
        facts_tool = FakeMcpTool(
            "get_machine_facts",
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "product_family": "CP-02S",
                            "max_power_budget": 160,
                        }
                    ),
                }
            ],
        )
        details_tool = FakeMcpTool(
            "get_port_details",
            [{"type": "text", "text": json.dumps({"ports": []})}],
        )
        gateway = McpDeviceGateway.from_tools(
            [info_tool, facts_tool, details_tool],
            profile="xdp",
            device_id="DEVICE-ALIAS",
        )

        status = await gateway.get_status()

        self.assertEqual(status["device_id"], "DEVICE-ALIAS")
        self.assertEqual(status["model"], "pro")
        self.assertEqual(status["machine"]["product_family"], "CP-02S")
        self.assertNotIn("psn", status)
        self.assertNotIn("ssid", status)
        self.assertNotIn("bssid", status)

    async def test_xdp_pd_status_uses_diagnostic_field_allowlist(self) -> None:
        pd_tool = FakeMcpTool(
            "get_port_pd_status",
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ports": [
                                {
                                    "port": 2,
                                    "pd_revision": "3.1",
                                    "pps_charging_supported": True,
                                    "device_name_en": "Test Device",
                                    "battery_vid": 1234,
                                    "cable_xid": 5678,
                                }
                            ]
                        }
                    ),
                }
            ],
        )
        gateway = McpDeviceGateway.from_tools(
            [pd_tool],
            profile="xdp",
            device_id="DEVICE-ALIAS",
        )

        status = await gateway.get_port_pd_status()
        port = status["ports"][0]

        self.assertEqual(port["port"], 2)
        self.assertEqual(port["pd_revision"], "3.1")
        self.assertNotIn("battery_vid", port)
        self.assertNotIn("cable_xid", port)


if __name__ == "__main__":
    unittest.main()

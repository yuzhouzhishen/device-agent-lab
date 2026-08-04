from __future__ import annotations

import json
import unittest
from typing import Any

from device_agent_lab.device_gateway import McpDeviceGateway
from device_agent_lab.xdp_probe import (
    redact_sensitive_text,
    redact_sensitive_values,
)


class FakeMcpTool:
    def __init__(self, name: str, result: object) -> None:
        self.name = name
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, arguments: dict[str, Any]) -> object:
        self.calls.append(arguments)
        return self.result


class XdpReadOnlyProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_only_calls_allowlisted_query_tools(self) -> None:
        query_tool = FakeMcpTool(
            "get_device_info",
            [{"type": "text", "text": json.dumps({"psn": "1234"})}],
        )
        control_tool = FakeMcpTool(
            "turn_off_port",
            [{"type": "text", "text": "should not be called"}],
        )
        gateway = McpDeviceGateway.from_tools(
            [query_tool, control_tool],
            profile="xdp",
            device_id="PRIVATE",
        )

        report = await gateway.probe_xdp_read_only()

        self.assertEqual(query_tool.calls, [{}])
        self.assertEqual(control_tool.calls, [])
        self.assertEqual(
            report["checks"]["get_device_info"]["status"],
            "ok",
        )
        self.assertEqual(
            report["checks"]["get_port_details"]["status"],
            "missing",
        )

    async def test_probe_rejects_non_xdp_profiles(self) -> None:
        gateway = McpDeviceGateway.from_tools(
            [],
            profile="lab",
            device_id="DEMO",
        )

        with self.assertRaisesRegex(RuntimeError, "xdp MCP profile"):
            await gateway.probe_xdp_read_only()


class ProbeRedactionTest(unittest.TestCase):
    def test_redacts_identifiers_and_network_credentials(self) -> None:
        report = {
            "psn": "123456789",
            "model": "CP02",
            "network": {
                "ssid": "private-wifi",
                "bssid": "00:11:22:33:44:55",
            },
            "endpoint_url": "https://private.example/token/mcp",
            "ports": [{"port": 1, "power_w": 20.0}],
        }

        redacted = redact_sensitive_values(report)

        self.assertEqual(redacted["psn"], "[REDACTED]")
        self.assertEqual(redacted["network"]["ssid"], "[REDACTED]")
        self.assertEqual(redacted["endpoint_url"], "[REDACTED]")
        self.assertEqual(redacted["model"], "CP02")
        self.assertEqual(redacted["ports"][0]["power_w"], 20.0)

    def test_redacts_credentials_embedded_in_error_text(self) -> None:
        error = (
            "request failed for "
            "https://mcp.example/1234567890123456/abcdef12/mcp "
            "from 00:11:22:33:44:55"
        )

        redacted = redact_sensitive_text(error)

        self.assertNotIn("1234567890123456", redacted)
        self.assertNotIn("abcdef12", redacted)
        self.assertNotIn("00:11:22:33:44:55", redacted)
        self.assertIn("[REDACTED_URL]", redacted)

    def test_does_not_treat_machine_as_a_mac_key(self) -> None:
        report = {
            "get_machine_facts": {
                "status": "ok",
                "data": {"product_family": "PRO"},
            }
        }

        redacted = redact_sensitive_values(report)

        self.assertEqual(
            redacted["get_machine_facts"]["data"]["product_family"],
            "PRO",
        )


if __name__ == "__main__":
    unittest.main()

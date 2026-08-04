from __future__ import annotations

import unittest

from device_agent_lab.device_gateway import McpDeviceGateway
from device_agent_lab.mcp_client_config import build_device_mcp_connection


class LocalMcpIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_state_persists_in_one_mcp_session(self) -> None:
        gateway = await McpDeviceGateway.connect(
            build_device_mcp_connection(),
            profile="lab",
            device_id="DEMO-CP02-001",
        )
        try:
            before = await gateway.get_port_status(2)
            await gateway.set_port_power(2, enabled=True)
            after = await gateway.get_port_status(2)
        finally:
            await gateway.aclose()

        self.assertEqual(before["mode"], "standby")
        self.assertEqual(after["mode"], "charging")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from device_agent_lab.mcp_client_config import (
    build_device_mcp_connection,
    build_remote_mcp_connection,
)


class MCPClientConfigTest(unittest.TestCase):
    def test_builds_stdio_connection_for_local_device_server(self) -> None:
        project_root = Path("/tmp/device-agent-lab")

        connection = build_device_mcp_connection(project_root)

        self.assertEqual(connection["transport"], "stdio")
        self.assertEqual(connection["command"], sys.executable)
        self.assertEqual(connection["args"], ["-m", "device_agent_lab.mcp_server"])
        self.assertEqual(connection["cwd"], str(project_root))

    def test_builds_streamable_http_connection_for_remote_xdp_server(self) -> None:
        connection = build_remote_mcp_connection(
            "https://mcp.example.invalid/device-id/token/mcp"
        )

        self.assertEqual(connection["transport"], "streamable_http")
        self.assertEqual(
            connection["url"],
            "https://mcp.example.invalid/device-id/token/mcp",
        )
        self.assertEqual(connection["timeout"], 20.0)

    def test_rejects_remote_url_without_an_mcp_transport_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "must end with /mcp or /sse"):
            build_remote_mcp_connection("https://mcp.example.invalid/device")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from device_agent_lab.agent_contracts import DeviceAgentContext, DeviceCommand
from device_agent_lab.command_executor import execute_device_command
from device_agent_lab.mock_device import DeviceController


class CommandExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )
        self.controller = DeviceController(psn="DEMO-CP02-001")

    def test_control_command_requires_confirmation_before_execution(self) -> None:
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id="DEMO-CP02-001",
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开2号端口。",
        )

        result = execute_device_command(command, self.context, self.controller)

        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")

    def test_confirmed_control_command_updates_device(self) -> None:
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id="DEMO-CP02-001",
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开2号端口。",
        )

        result = execute_device_command(
            command,
            self.context,
            self.controller,
            confirmed=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["device"]["ports"]["2"]["mode"], "charging")

    def test_query_command_returns_port_status(self) -> None:
        command = DeviceCommand(
            intent="query_device",
            action="get_port_status",
            device_id="DEMO-CP02-001",
            port=1,
            need_confirmation=False,
            reason="用户询问1号端口状态。",
        )

        result = execute_device_command(command, self.context, self.controller)

        self.assertTrue(result["ok"])
        self.assertEqual(result["port"]["port_id"], 1)


if __name__ == "__main__":
    unittest.main()

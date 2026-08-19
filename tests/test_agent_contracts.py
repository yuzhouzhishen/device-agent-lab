from __future__ import annotations

import unittest

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    build_context_prompt,
    validate_command_against_context,
)


class AgentContractsTest(unittest.TestCase):
    def test_context_prompt_exposes_device_boundary(self) -> None:
        context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )

        prompt = build_context_prompt(context)

        self.assertIn("DEMO-CP02-001", prompt)
        self.assertIn("allowed_ports: 1, 2, 3", prompt)
        self.assertIn("必须输出结构化动作", prompt)
        self.assertIn("respond_chat", prompt)
        self.assertIn("answer_knowledge", prompt)

    def test_validates_control_command_against_context(self) -> None:
        context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id="DEMO-CP02-001",
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开2号端口。",
        )

        validate_command_against_context(command, context)

    def test_rejects_unknown_port_for_control_command(self) -> None:
        context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id="DEMO-CP02-001",
            port=9,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开9号端口。",
        )

        with self.assertRaises(ValueError) as error:
            validate_command_against_context(command, context)

        self.assertIn("port 9 is not available", str(error.exception))

    def test_chat_remains_available_when_device_is_offline(self) -> None:
        context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=False,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )
        command = DeviceCommand(
            intent="chat",
            action="respond_chat",
            device_id="DEMO-CP02-001",
            need_confirmation=False,
            reason="回答身份问题。",
            reply="我是 DeviceOps。",
        )

        validate_command_against_context(command, context)


if __name__ == "__main__":
    unittest.main()

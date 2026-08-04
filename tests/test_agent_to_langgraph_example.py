from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from device_agent_lab.agent_contracts import DeviceAgentContext, DeviceCommand
from device_agent_lab.mock_device import DeviceController


EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "07_agent_to_langgraph.py"
)


def load_example() -> dict[str, object]:
    if not EXAMPLE_PATH.exists():
        raise AssertionError("missing integration example: 07_agent_to_langgraph.py")
    module_name = "agent_to_langgraph_example"
    spec = importlib.util.spec_from_file_location(module_name, EXAMPLE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load 07_agent_to_langgraph.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return vars(module)


class AgentToLangGraphExampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )
        self.controller = DeviceController(psn=self.context.device_id)

    def test_query_command_finishes_without_requesting_approval(self) -> None:
        example = load_example()
        command = DeviceCommand(
            intent="query_device",
            action="get_port_status",
            device_id=self.context.device_id,
            port=2,
            need_confirmation=False,
            reason="用户查询2号端口状态。",
        )

        def unexpected_approval(_: dict[str, object]) -> bool:
            raise AssertionError("query command must not request approval")

        confirmation, completed = example["run_command_workflow"](
            command,
            self.context,
            self.controller,
            unexpected_approval,
        )

        self.assertIsNone(confirmation)
        self.assertEqual(completed["summary"], "端口 2 当前状态为 standby。")
        self.assertEqual(completed["trace"], ["validate", "query", "summarize"])

    def test_control_command_uses_approval_before_execution(self) -> None:
        example = load_example()
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开2号端口。",
        )
        approval_requests: list[dict[str, object]] = []

        def approve(request: dict[str, object]) -> bool:
            approval_requests.append(request)
            return True

        confirmation, completed = example["run_command_workflow"](
            command,
            self.context,
            self.controller,
            approve,
        )

        self.assertEqual(confirmation, approval_requests[0])
        self.assertEqual(confirmation["type"], "device_control_confirmation")
        self.assertEqual(self.controller.get_port_status(2)["mode"], "charging")
        self.assertEqual(completed["trace"], ["validate", "control", "summarize"])


if __name__ == "__main__":
    unittest.main()

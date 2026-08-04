from __future__ import annotations

import importlib
import unittest

from langgraph.types import Command

from device_agent_lab.agent_contracts import DeviceAgentContext, DeviceCommand
from device_agent_lab.mock_device import DeviceController


class DeviceWorkflowModuleTest(unittest.TestCase):
    def test_workflow_module_exposes_builder(self) -> None:
        module = importlib.import_module("device_agent_lab.device_workflow")

        self.assertTrue(hasattr(module, "build_device_workflow"))


class DeviceWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="实习调试员",
        )
        self.controller = DeviceController(psn=self.context.device_id)

    def test_query_runs_without_confirmation(self) -> None:
        from device_agent_lab.device_workflow import build_device_workflow

        graph = build_device_workflow(self.controller)
        command = DeviceCommand(
            intent="query_device",
            action="get_port_status",
            device_id=self.context.device_id,
            port=1,
            need_confirmation=False,
            reason="用户询问1号端口状态。",
        )

        result = graph.invoke(
            {
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "query-demo"}},
        )

        self.assertTrue(result["result"]["ok"])
        self.assertEqual(result["result"]["port"]["port_id"], 1)
        self.assertEqual(result["trace"], ["validate", "query", "summarize"])

    def test_whole_device_query_summarizes_without_a_port(self) -> None:
        from device_agent_lab.device_workflow import build_device_workflow

        graph = build_device_workflow(self.controller)
        command = DeviceCommand(
            intent="query_device",
            action="get_status",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="用户询问设备整体状态。",
        )

        result = graph.invoke(
            {
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "device-query-demo"}},
        )

        self.assertTrue(result["result"]["ok"])
        self.assertEqual(result["summary"], "设备查询已完成。")

    def test_control_pauses_until_confirmation_then_resumes(self) -> None:
        from device_agent_lab.device_workflow import build_device_workflow

        graph = build_device_workflow(self.controller)
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开2号端口。",
        )
        config = {"configurable": {"thread_id": "control-demo"}}

        paused = graph.invoke(
            {
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config=config,
        )

        self.assertIn("__interrupt__", paused)
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")

        resumed = graph.invoke(Command(resume=True), config=config)

        self.assertTrue(resumed["result"]["ok"])
        self.assertEqual(self.controller.get_port_status(2)["mode"], "charging")
        self.assertEqual(resumed["trace"], ["validate", "control", "summarize"])

    def test_rejected_control_does_not_update_device(self) -> None:
        from device_agent_lab.device_workflow import build_device_workflow

        graph = build_device_workflow(self.controller)
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="用户要求打开2号端口。",
        )
        config = {"configurable": {"thread_id": "reject-demo"}}
        graph.invoke(
            {
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config=config,
        )

        rejected = graph.invoke(Command(resume=False), config=config)

        self.assertTrue(rejected["result"]["cancelled"])
        self.assertEqual(rejected["summary"], "控制操作已取消。")
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")

    def test_clarification_uses_its_own_branch(self) -> None:
        from device_agent_lab.device_workflow import build_device_workflow

        graph = build_device_workflow(self.controller)
        command = DeviceCommand(
            intent="clarify",
            action="ask_clarification",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="请说明要操作哪个端口。",
        )

        result = graph.invoke(
            {
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "clarify-demo"}},
        )

        self.assertEqual(result["summary"], "请说明要操作哪个端口。")
        self.assertEqual(result["trace"], ["validate", "clarify", "summarize"])


if __name__ == "__main__":
    unittest.main()

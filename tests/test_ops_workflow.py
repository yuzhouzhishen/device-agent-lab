from __future__ import annotations

import unittest

from langgraph.types import Command

from device_agent_lab.agent_contracts import DeviceAgentContext, DeviceCommand
from device_agent_lab.device_gateway import (
    DeviceGatewayError,
    MockDeviceGateway,
)
from device_agent_lab.mock_device import DeviceController
from device_agent_lab.ops_workflow import build_ops_workflow


class PartialFailureGateway(MockDeviceGateway):
    async def get_port_pd_status(self) -> dict:
        raise DeviceGatewayError("PD telemetry unavailable")


class PrecheckFailureGateway(MockDeviceGateway):
    def __init__(self, controller: DeviceController) -> None:
        super().__init__(controller)
        self.control_calls = 0

    async def get_port_status(self, port_id: int) -> dict:
        del port_id
        raise DeviceGatewayError("precheck unavailable")

    async def set_port_power(
        self,
        port_id: int,
        *,
        enabled: bool,
        power_w: float | None = None,
    ) -> dict:
        self.control_calls += 1
        return await super().set_port_power(
            port_id,
            enabled=enabled,
            power_w=power_w,
        )


class PostcheckFailureGateway(MockDeviceGateway):
    def __init__(self, controller: DeviceController) -> None:
        super().__init__(controller)
        self.status_calls = 0

    async def get_port_status(self, port_id: int) -> dict:
        self.status_calls += 1
        if self.status_calls > 1:
            raise DeviceGatewayError("postcheck unavailable")
        return await super().get_port_status(port_id)


class DeviceOpsWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="operator",
        )
        self.controller = DeviceController(psn=self.context.device_id)
        self.gateway = MockDeviceGateway(self.controller)

    async def test_query_uses_device_gateway_without_confirmation(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="query_device",
            action="get_port_status",
            device_id=self.context.device_id,
            port=1,
            need_confirmation=False,
            reason="查询1号端口。",
        )

        result = await graph.ainvoke(
            {
                "request": "1号端口现在怎么样？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-query"}},
        )

        self.assertEqual(result["result"]["port"]["port_id"], 1)
        self.assertEqual(
            result["trace"],
            ["validate", "retrieve", "query", "summarize"],
        )

    async def test_query_retrieves_relevant_device_guidance(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="query_device",
            action="get_port_status",
            device_id=self.context.device_id,
            port=2,
            need_confirmation=False,
            reason="排查2号端口无法充电。",
        )

        result = await graph.ainvoke(
            {
                "request": "2号端口为什么无法充电？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-diagnose"}},
        )

        self.assertEqual(result["knowledge"][0]["id"], "port-not-charging")
        self.assertIn("参考排查", result["summary"])

    async def test_diagnosis_collects_multiple_tool_results(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="query_device",
            action="diagnose_port",
            device_id=self.context.device_id,
            port=2,
            need_confirmation=False,
            reason="诊断2号端口无法充电。",
        )

        result = await graph.ainvoke(
            {
                "request": "2号端口为什么无法充电？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-multi-diagnose"}},
        )

        diagnosis = result["result"]["diagnosis"]
        self.assertEqual(diagnosis["health"], "disconnected")
        self.assertEqual(
            diagnosis["evidence"]["port_status"]["status"],
            "ok",
        )
        self.assertEqual(
            result["trace"],
            [
                "validate",
                "retrieve",
                "collect_port_status",
                "collect_charging_status",
                "collect_pd_status",
                "collect_temperature_mode",
                "analyze_diagnosis",
                "summarize",
            ],
        )
        self.assertIn("未检测到连接设备", result["summary"])

    async def test_diagnosis_degrades_when_pd_evidence_fails(self) -> None:
        gateway = PartialFailureGateway(self.controller)
        graph = build_ops_workflow(gateway)
        command = DeviceCommand(
            intent="query_device",
            action="diagnose_port",
            device_id=self.context.device_id,
            port=1,
            need_confirmation=False,
            reason="诊断1号端口。",
        )

        result = await graph.ainvoke(
            {
                "request": "诊断1号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-partial-diagnose"}},
        )

        diagnosis = result["result"]["diagnosis"]
        self.assertTrue(result["result"]["ok"])
        self.assertEqual(
            diagnosis["evidence"]["pd_status"]["status"],
            "error",
        )
        self.assertTrue(
            any(
                "pd_status 证据不可用" in finding
                for finding in diagnosis["findings"]
            )
        )

    async def test_control_waits_for_confirmation_before_using_gateway(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="打开2号端口。",
        )
        config = {"configurable": {"thread_id": "ops-control"}}

        paused = await graph.ainvoke(
            {
                "request": "打开2号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config=config,
        )

        self.assertIn("__interrupt__", paused)
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")

        completed = await graph.ainvoke(Command(resume=True), config=config)

        self.assertEqual(self.controller.get_port_status(2)["mode"], "charging")
        self.assertEqual(
            completed["result"]["verification"]["status"],
            "verified",
        )
        self.assertEqual(
            completed["trace"],
            [
                "validate",
                "retrieve",
                "prepare_control",
                "control",
                "verify_control",
                "summarize",
            ],
        )

    async def test_control_cancellation_does_not_change_device(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="打开2号端口。",
        )
        config = {"configurable": {"thread_id": "ops-cancel"}}

        paused = await graph.ainvoke(
            {
                "request": "打开2号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config=config,
        )
        completed = await graph.ainvoke(Command(resume=False), config=config)

        self.assertIn("__interrupt__", paused)
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")
        self.assertTrue(completed["result"]["cancelled"])
        self.assertEqual(
            completed["trace"],
            [
                "validate",
                "retrieve",
                "prepare_control",
                "control",
                "summarize",
            ],
        )

    async def test_control_is_blocked_when_precheck_fails(self) -> None:
        gateway = PrecheckFailureGateway(self.controller)
        graph = build_ops_workflow(gateway)
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="打开2号端口。",
        )

        result = await graph.ainvoke(
            {
                "request": "打开2号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-precheck-failure"}},
        )

        self.assertNotIn("__interrupt__", result)
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["result"]["stage"], "precheck")
        self.assertEqual(gateway.control_calls, 0)
        self.assertEqual(
            result["trace"],
            ["validate", "retrieve", "prepare_control", "summarize"],
        )

    async def test_control_reports_postcheck_failure_without_repeating_command(
        self,
    ) -> None:
        gateway = PostcheckFailureGateway(self.controller)
        graph = build_ops_workflow(gateway)
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=2,
            enabled=True,
            need_confirmation=True,
            reason="打开2号端口。",
        )
        config = {"configurable": {"thread_id": "ops-postcheck-failure"}}

        await graph.ainvoke(
            {
                "request": "打开2号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config=config,
        )
        completed = await graph.ainvoke(Command(resume=True), config=config)

        self.assertTrue(completed["result"]["ok"])
        self.assertEqual(
            completed["result"]["verification"]["status"],
            "error",
        )
        self.assertEqual(gateway.status_calls, 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.audit import InMemoryAuditLog
from device_agent_lab.device_gateway import DeviceGatewayError, MockDeviceGateway
from device_agent_lab.device_ops_service import DeviceOpsService
from device_agent_lab.mock_device import DeviceController
from device_agent_lab.planner import RuleBasedCommandPlanner


class OfflineGateway:
    async def get_status(self) -> dict:
        raise DeviceGatewayError("device is offline")

    async def get_port_status(self, port_id: int) -> dict:
        del port_id
        raise DeviceGatewayError("device is offline")

    async def set_port_power(
        self,
        port_id: int,
        *,
        enabled: bool,
        power_w: float | None = None,
    ) -> dict:
        del port_id, enabled, power_w
        raise DeviceGatewayError("device is offline")


class DeviceOpsServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="operator",
        )
        self.controller = DeviceController(psn=self.context.device_id)
        self.audit_log = InMemoryAuditLog()
        self.service = DeviceOpsService(
            planner=RuleBasedCommandPlanner(),
            gateway=MockDeviceGateway(self.controller),
            audit_log=self.audit_log,
        )

    async def test_query_completes_through_the_public_service_interface(self) -> None:
        run = await self.service.start(
            "查询1号端口状态",
            self.context,
            thread_id="service-query",
        )

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.command.action, "get_port_status")
        self.assertEqual(run.result["port"]["port_id"], 1)
        self.assertEqual(self.audit_log.records[0].status, "completed")
        self.assertEqual(
            self.audit_log.records[0].command["action"],
            "get_port_status",
        )

    async def test_control_requires_resume_and_records_user_decision(self) -> None:
        pending = await self.service.start(
            "打开2号端口",
            self.context,
            thread_id="service-control",
        )

        self.assertEqual(pending.status, "confirmation_required")
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")

        completed = await self.service.resume(
            pending.thread_id,
            approved=True,
        )

        self.assertEqual(completed.status, "completed")
        self.assertTrue(completed.approved)
        self.assertEqual(self.controller.get_port_status(2)["mode"], "charging")
        self.assertEqual(
            [record.status for record in self.audit_log.records],
            ["confirmation_required", "completed"],
        )
        self.assertTrue(self.audit_log.records[-1].approved)

    async def test_backend_failure_becomes_an_audited_application_result(self) -> None:
        service = DeviceOpsService(
            planner=RuleBasedCommandPlanner(),
            gateway=OfflineGateway(),
            audit_log=self.audit_log,
        )

        run = await service.start(
            "查询设备状态",
            self.context,
            thread_id="service-offline",
        )

        self.assertEqual(run.status, "completed")
        self.assertFalse(run.result["ok"])
        self.assertIn("offline", run.summary)
        self.assertFalse(self.audit_log.records[-1].result["ok"])

    async def test_charging_port_question_only_returns_port_numbers(self) -> None:
        run = await self.service.start(
            "现在几号端口在充电",
            self.context,
            conversation_id="conversation-summary",
        )

        self.assertEqual(run.command.action, "get_status")
        self.assertEqual(run.summary, "1号端口。")

    async def test_user_correction_only_returns_connected_ports(self) -> None:
        run = await self.service.start(
            "我只是问几号端口在线，多余内容不要出现",
            self.context,
            conversation_id="conversation-correction",
        )

        self.assertEqual(run.command.action, "get_status")
        self.assertEqual(run.summary, "1号端口。")

    async def test_overall_status_still_returns_operational_details(self) -> None:
        run = await self.service.start(
            "查询设备整体状态",
            self.context,
            conversation_id="conversation-overview",
        )

        self.assertIn("设备在线，共 3 个端口", run.summary)
        self.assertIn("1 号端口正在充电", run.summary)
        self.assertIn("45.00 W", run.summary)

    async def test_follow_up_control_resolves_last_active_port(self) -> None:
        conversation_id = "conversation-follow-up"
        first = await self.service.start(
            "现在几号端口在充电",
            self.context,
            conversation_id=conversation_id,
        )
        pending = await self.service.start(
            "把它关掉",
            self.context,
            conversation_id=conversation_id,
        )

        self.assertEqual(first.summary, "1号端口。")
        self.assertEqual(pending.status, "confirmation_required")
        self.assertEqual(pending.command.port, 1)
        self.assertFalse(pending.command.enabled)
        self.assertEqual(pending.conversation_id, conversation_id)

        cancelled = await self.service.resume(
            pending.thread_id,
            approved=False,
        )
        self.assertEqual(cancelled.conversation_id, conversation_id)
        self.assertTrue(cancelled.result["cancelled"])

    async def test_ambiguous_follow_up_control_requests_port(self) -> None:
        self.controller.set_port_power(2, enabled=True)
        conversation_id = "conversation-ambiguous"
        first = await self.service.start(
            "现在几号端口在充电",
            self.context,
            conversation_id=conversation_id,
        )
        follow_up = await self.service.start(
            "把它关掉",
            self.context,
            conversation_id=conversation_id,
        )

        self.assertEqual(first.summary, "1号、2号端口。")
        self.assertEqual(follow_up.command.action, "ask_clarification")
        self.assertIn("哪个端口", follow_up.summary)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.audit import InMemoryAuditLog
from device_agent_lab.conversation_store import SqliteConversationStore
from device_agent_lab.device_gateway import DeviceGatewayError, MockDeviceGateway
from device_agent_lab.device_ops_service import DeviceOpsService
from device_agent_lab.metrics import TokenPricing, TokenUsage
from device_agent_lab.mock_device import DeviceController
from device_agent_lab.planner import PlanResult, RuleBasedCommandPlanner


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


class MeteredPlanner:
    """Rule-based planning that reports usage, standing in for a real model."""

    def __init__(self) -> None:
        self._inner = RuleBasedCommandPlanner()

    async def plan(self, request, context, conversation=None) -> PlanResult:
        plan = await self._inner.plan(request, context, conversation)
        return PlanResult(
            command=plan.command,
            usage=TokenUsage(
                input_tokens=800,
                output_tokens=200,
                total_tokens=1000,
            ),
            model="stub-model",
        )


class RunCostTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="operator",
        )

    def _service(self, pricing: TokenPricing | None) -> DeviceOpsService:
        return DeviceOpsService(
            planner=MeteredPlanner(),
            gateway=MockDeviceGateway(DeviceController()),
            pricing=pricing,
        )

    async def test_priced_run_reports_money_alongside_tokens(self) -> None:
        service = self._service(
            TokenPricing(input_per_1m=2.0, output_per_1m=10.0, currency="CNY")
        )

        run = await service.start(
            "查询1号端口状态",
            self.context,
            thread_id="cost-priced",
        )
        metrics = run.metrics

        self.assertIsNotNone(metrics)
        assert metrics is not None and metrics.usage is not None
        self.assertEqual(metrics.usage.total_tokens, 1000)
        self.assertEqual(metrics.model, "stub-model")
        # 800 in @ 2.0/1M + 200 out @ 10.0/1M
        self.assertAlmostEqual(metrics.cost, 0.0036)
        self.assertEqual(metrics.currency, "CNY")

    async def test_unpriced_run_reports_tokens_but_no_cost(self) -> None:
        run = await self._service(None).start(
            "查询1号端口状态",
            self.context,
            thread_id="cost-unpriced",
        )
        metrics = run.metrics

        assert metrics is not None and metrics.usage is not None
        self.assertEqual(metrics.usage.total_tokens, 1000)
        self.assertIsNone(metrics.cost)
        self.assertIsNone(metrics.currency)

    async def test_resume_does_not_bill_planning_twice(self) -> None:
        service = self._service(
            TokenPricing(input_per_1m=2.0, output_per_1m=10.0)
        )
        pending = await service.start(
            "打开2号端口",
            self.context,
            thread_id="cost-resume",
        )
        resumed = await service.resume("cost-resume", approved=True)

        assert pending.metrics is not None and resumed.metrics is not None
        self.assertIsNotNone(pending.metrics.cost)
        # Resuming replans nothing, so the model is not charged again.
        self.assertIsNone(resumed.metrics.usage)
        self.assertIsNone(resumed.metrics.cost)
        self.assertEqual(resumed.metrics.planner_ms, 0.0)


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

    async def test_follow_up_suggestions_follow_the_explicit_port(self) -> None:
        run = await self.service.start(
            "查询2号端口状态",
            self.context,
            conversation_id="suggest-explicit-port",
        )

        self.assertEqual(
            [suggestion.label for suggestion in run.suggestions],
            ["诊断 2 号端口", "解释本次结果", "查看设备整体状态"],
        )
        self.assertTrue(
            all("1 号端口" not in item.label for item in run.suggestions)
        )
        self.assertTrue(
            all(item.kind != "control" for item in run.suggestions)
        )

    async def test_business_chat_completes_without_device_operation(self) -> None:
        run = await self.service.start(
            "你是谁",
            self.context,
            conversation_id="conversation-chat",
        )

        self.assertEqual(run.command.action, "respond_chat")
        self.assertIn("DeviceOps", run.summary)
        self.assertEqual(run.result["response_kind"], "conversation")
        self.assertEqual(
            run.trace,
            ["validate", "respond_chat", "summarize"],
        )

    async def test_unsupported_request_explains_scope(self) -> None:
        run = await self.service.start(
            "今天天气怎么样？",
            self.context,
            conversation_id="conversation-unsupported",
        )

        self.assertEqual(run.command.action, "unsupported_request")
        self.assertIn("设备运维范围", run.summary)
        self.assertTrue(run.result["ok"])
        self.assertEqual(run.suggestions, [])

    async def test_control_requires_resume_and_records_user_decision(self) -> None:
        pending = await self.service.start(
            "打开2号端口",
            self.context,
            thread_id="service-control",
        )

        self.assertEqual(pending.status, "confirmation_required")
        self.assertEqual(self.controller.get_port_status(2)["mode"], "standby")
        self.assertEqual(pending.suggestions, [])

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
        self.assertEqual(
            completed.suggestions[0].label,
            "查看 2 号端口状态",
        )

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
        self.assertNotIn("设备操作失败", follow_up.summary)

    async def test_explains_previous_result_from_conversation_memory(self) -> None:
        conversation_id = "conversation-explain"
        first = await self.service.start(
            "查询2号端口状态",
            self.context,
            conversation_id=conversation_id,
        )
        explanation = await self.service.start(
            "刚才为什么这么判断？",
            self.context,
            conversation_id=conversation_id,
        )

        self.assertEqual(first.command.action, "get_port_status")
        self.assertEqual(explanation.command.action, "explain_previous")
        self.assertIn("端口状态查询", explanation.summary)
        self.assertIn("端口=2", explanation.summary)

    async def test_follow_up_context_survives_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "sessions.sqlite3"
            first_service = DeviceOpsService(
                planner=RuleBasedCommandPlanner(),
                gateway=MockDeviceGateway(self.controller),
                conversation_store=SqliteConversationStore(path),
            )
            await first_service.start(
                "查询2号端口状态",
                self.context,
                conversation_id="persistent-conversation",
            )
            restarted_service = DeviceOpsService(
                planner=RuleBasedCommandPlanner(),
                gateway=MockDeviceGateway(self.controller),
                conversation_store=SqliteConversationStore(path),
            )

            pending = await restarted_service.start(
                "把它关掉",
                self.context,
                conversation_id="persistent-conversation",
            )

        self.assertEqual(pending.command.action, "set_port_power")
        self.assertEqual(pending.command.port, 2)
        self.assertFalse(pending.command.enabled)


if __name__ == "__main__":
    unittest.main()

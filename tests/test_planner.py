from __future__ import annotations

import unittest

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    DeviceConversationContext,
)
from device_agent_lab.planner import (
    RuleBasedCommandPlanner,
    apply_explicit_response_preferences,
)


class RuleBasedCommandPlannerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="operator",
        )
        self.planner = RuleBasedCommandPlanner()

    async def test_plans_confirmed_control_shape_from_natural_language(self) -> None:
        command = await self.planner.plan("请打开2号端口", self.context)

        self.assertEqual(command.action, "set_port_power")
        self.assertEqual(command.port, 2)
        self.assertTrue(command.enabled)
        self.assertTrue(command.need_confirmation)

    async def test_plans_port_query_for_diagnostic_question(self) -> None:
        command = await self.planner.plan(
            "2号端口为什么无法充电？",
            self.context,
        )

        self.assertEqual(command.action, "diagnose_port")
        self.assertEqual(command.port, 2)
        self.assertFalse(command.need_confirmation)

    async def test_requests_clarification_when_control_has_no_port(self) -> None:
        command = await self.planner.plan("帮我关闭一个端口", self.context)

        self.assertEqual(command.action, "ask_clarification")
        self.assertIn("端口", command.reason)

    async def test_resolves_pronoun_from_conversation_context(self) -> None:
        command = await self.planner.plan(
            "把它关掉",
            self.context,
            DeviceConversationContext(
                last_port=1,
                charging_ports=[1],
            ),
        )

        self.assertEqual(command.action, "set_port_power")
        self.assertEqual(command.port, 1)
        self.assertFalse(command.enabled)

    async def test_pronoun_without_unique_context_requests_clarification(
        self,
    ) -> None:
        command = await self.planner.plan(
            "把它关掉",
            self.context,
            DeviceConversationContext(charging_ports=[1, 2]),
        )

        self.assertEqual(command.action, "ask_clarification")
        self.assertIn("哪个端口", command.reason)

    async def test_charging_port_question_requests_a_concise_port_list(
        self,
    ) -> None:
        command = await self.planner.plan(
            "现在几号端口在充电",
            self.context,
        )

        self.assertEqual(command.action, "get_status")
        self.assertEqual(command.response_focus, "charging_ports")
        self.assertEqual(command.response_detail, "concise")

    async def test_user_correction_requests_only_connected_ports(self) -> None:
        command = await self.planner.plan(
            "我只是问几号端口在线，多余内容不要出现",
            self.context,
        )

        self.assertEqual(command.action, "get_status")
        self.assertEqual(command.response_focus, "connected_ports")
        self.assertEqual(command.response_detail, "concise")

    async def test_overall_status_keeps_the_standard_overview(self) -> None:
        command = await self.planner.plan(
            "查询设备整体状态",
            self.context,
        )

        self.assertEqual(command.action, "get_status")
        self.assertEqual(command.response_focus, "device_overview")
        self.assertEqual(command.response_detail, "standard")

    def test_explicit_format_request_overrides_model_output(self) -> None:
        model_command = DeviceCommand(
            intent="query_device",
            action="get_status",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="查询设备状态。",
            response_focus="device_overview",
            response_detail="standard",
        )

        normalized = apply_explicit_response_preferences(
            "现在几号端口在充电",
            model_command,
        )

        self.assertEqual(normalized.response_focus, "charging_ports")
        self.assertEqual(normalized.response_detail, "concise")


if __name__ == "__main__":
    unittest.main()

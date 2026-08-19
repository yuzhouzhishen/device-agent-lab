from __future__ import annotations

import json
import unittest

import httpx

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    ConversationTurn,
    DeviceConversationContext,
)
from device_agent_lab.planner import (
    FastPathCommandPlanner,
    FallbackCommandPlanner,
    OllamaCommandPlanner,
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

    async def _plan(
        self,
        request: str,
        context: DeviceAgentContext,
        conversation: DeviceConversationContext | None = None,
    ) -> DeviceCommand:
        """These cases assert on the command; usage is covered separately."""
        return (
            await self.planner.plan(request, context, conversation)
        ).command

    async def test_plans_confirmed_control_shape_from_natural_language(self) -> None:
        command = await self._plan("请打开2号端口", self.context)

        self.assertEqual(command.action, "set_port_power")
        self.assertEqual(command.port, 2)
        self.assertTrue(command.enabled)
        self.assertTrue(command.need_confirmation)

    async def test_plans_port_query_for_diagnostic_question(self) -> None:
        command = await self._plan(
            "2号端口为什么无法充电？",
            self.context,
        )

        self.assertEqual(command.action, "diagnose_port")
        self.assertEqual(command.port, 2)
        self.assertFalse(command.need_confirmation)

    async def test_plans_whole_device_diagnosis_without_a_port(self) -> None:
        command = await self._plan(
            "帮我检查设备当前有没有异常",
            self.context,
        )

        self.assertEqual(command.action, "diagnose_device")
        self.assertIsNone(command.port)

    async def test_diagnostic_follow_up_reuses_the_last_port(self) -> None:
        command = await self._plan(
            "那应该怎么处理？",
            self.context,
            DeviceConversationContext(
                last_port=3,
                recent_turns=[
                    ConversationTurn(
                        request="诊断3号端口",
                        summary="3号端口协商异常。",
                        action="diagnose_port",
                        port=3,
                    )
                ],
            ),
        )

        self.assertEqual(command.action, "diagnose_port")
        self.assertEqual(command.port, 3)

    async def test_requests_clarification_when_control_has_no_port(self) -> None:
        command = await self._plan("帮我关闭一个端口", self.context)

        self.assertEqual(command.action, "ask_clarification")
        self.assertIn("端口", command.reason)

    async def test_resolves_pronoun_from_conversation_context(self) -> None:
        command = await self._plan(
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
        command = await self._plan(
            "把它关掉",
            self.context,
            DeviceConversationContext(charging_ports=[1, 2]),
        )

        self.assertEqual(command.action, "ask_clarification")
        self.assertIn("哪个端口", command.reason)

    async def test_charging_port_question_requests_a_concise_port_list(
        self,
    ) -> None:
        command = await self._plan(
            "现在几号端口在充电",
            self.context,
        )

        self.assertEqual(command.action, "get_status")
        self.assertEqual(command.response_focus, "charging_ports")
        self.assertEqual(command.response_detail, "concise")

    async def test_user_correction_requests_only_connected_ports(self) -> None:
        command = await self._plan(
            "我只是问几号端口在线，多余内容不要出现",
            self.context,
        )

        self.assertEqual(command.action, "get_status")
        self.assertEqual(command.response_focus, "connected_ports")
        self.assertEqual(command.response_detail, "concise")

    async def test_overall_status_keeps_the_standard_overview(self) -> None:
        command = await self._plan(
            "查询设备整体状态",
            self.context,
        )

        self.assertEqual(command.action, "get_status")
        self.assertEqual(command.response_focus, "device_overview")
        self.assertEqual(command.response_detail, "standard")

    async def test_routes_identity_question_to_business_chat(self) -> None:
        command = await self._plan("你是谁", self.context)

        self.assertEqual(command.intent, "chat")
        self.assertEqual(command.action, "respond_chat")
        self.assertIn("DeviceOps", command.reply)

    async def test_routes_firmware_question_to_knowledge(self) -> None:
        command = await self._plan(
            "PD 3.0 协议是什么？",
            self.context,
        )

        self.assertEqual(command.intent, "knowledge_query")
        self.assertEqual(command.action, "answer_knowledge")

    async def test_routes_esp_idf_nvs_question_to_knowledge(self) -> None:
        command = await self._plan(
            "ESP-IDF 中 NVS 初始化失败应该怎么处理？",
            self.context,
        )

        self.assertEqual(command.intent, "knowledge_query")
        self.assertEqual(command.action, "answer_knowledge")

    async def test_routes_unrelated_question_out_of_scope(self) -> None:
        command = await self._plan("今天天气怎么样？", self.context)

        self.assertEqual(command.intent, "unsupported")
        self.assertEqual(command.action, "unsupported_request")
        self.assertIn("设备运维范围", command.reply)

    async def test_routes_previous_result_question_to_explanation(self) -> None:
        command = await self._plan(
            "刚才为什么这么判断？",
            self.context,
            DeviceConversationContext(
                recent_turns=[
                    ConversationTurn(
                        request="诊断3号端口",
                        summary="3号端口协商异常。",
                        action="diagnose_port",
                        port=3,
                        evidence=["能力不匹配"],
                    )
                ]
            ),
        )

        self.assertEqual(command.intent, "chat")
        self.assertEqual(command.action, "explain_previous")

    async def test_short_why_question_explains_the_previous_turn(self) -> None:
        command = await self._plan(
            "为什么？",
            self.context,
            DeviceConversationContext(
                recent_turns=[
                    ConversationTurn(
                        request="查询3号端口",
                        summary="3号端口正在充电。",
                        action="get_port_status",
                        port=3,
                    )
                ]
            ),
        )

        self.assertEqual(command.action, "explain_previous")

    async def test_current_temperature_mode_uses_live_diagnosis(self) -> None:
        command = await self._plan(
            "当前温控模式是什么？",
            self.context,
        )

        self.assertEqual(command.action, "diagnose_device")

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

    def test_document_analysis_requests_a_detailed_response(self) -> None:
        model_command = DeviceCommand(
            intent="query_device",
            action="diagnose_port",
            device_id=self.context.device_id,
            port=3,
            need_confirmation=False,
            reason="诊断端口。",
            response_focus="diagnosis",
            response_detail="concise",
        )

        normalized = apply_explicit_response_preferences(
            "3号端口功率异常，结合文档分析",
            model_command,
        )

        self.assertEqual(normalized.response_detail, "detailed")


class FailingPlanner:
    async def plan(self, request, context, conversation=None):
        del request, context, conversation
        raise httpx.ConnectError("ollama unavailable")


class ModelPlannerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = DeviceAgentContext(
            device_id="DEMO-CP02-001",
            online=True,
            allowed_ports=[1, 2, 3],
            operator_role="operator",
        )

    async def test_ollama_planner_uses_json_schema_output(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.update(payload)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "query_device",
                                "action": "get_port_status",
                                "device_id": "model-invented-id",
                                "need_confirmation": True,
                                "reason": "charging_ports",
                                "response_focus": "port_status",
                                "response_detail": "standard",
                            }
                        )
                    },
                    "prompt_eval_count": 120,
                    "eval_count": 24,
                },
            )

        planner = OllamaCommandPlanner(
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(planner.aclose)

        result = await planner.plan("现在几号端口在充电", self.context)

        self.assertEqual(result.command.action, "get_status")
        self.assertEqual(result.command.device_id, self.context.device_id)
        self.assertEqual(result.command.response_focus, "charging_ports")
        self.assertFalse(result.command.need_confirmation)
        self.assertEqual(result.usage.total_tokens, 144)
        self.assertEqual(
            captured["format"]["properties"]["action"]["type"],
            "string",
        )
        self.assertFalse(captured["stream"])

    async def test_model_failure_falls_back_to_rules_and_is_visible(
        self,
    ) -> None:
        planner = FallbackCommandPlanner(FailingPlanner())

        result = await planner.plan("查询2号端口", self.context)

        self.assertEqual(result.command.action, "get_port_status")
        self.assertTrue(result.degraded)
        self.assertEqual(result.model, "rules-fallback")
        self.assertIn("ConnectError", result.warning)

    async def test_fast_path_answers_identity_without_calling_model(
        self,
    ) -> None:
        planner = FastPathCommandPlanner(FailingPlanner())

        result = await planner.plan("你是谁", self.context)

        self.assertEqual(result.command.action, "respond_chat")
        self.assertEqual(result.model, "rules-fast-path")
        self.assertIn("DeviceOps", result.command.reply)

    async def test_fast_path_handles_explicit_device_query_without_model(
        self,
    ) -> None:
        planner = FastPathCommandPlanner(FailingPlanner())

        result = await planner.plan("现在几号端口在充电", self.context)

        self.assertEqual(result.command.action, "get_status")
        self.assertEqual(result.command.response_focus, "charging_ports")
        self.assertEqual(result.model, "rules-fast-path")

    async def test_fast_path_handles_explicit_control_without_model(
        self,
    ) -> None:
        planner = FastPathCommandPlanner(FailingPlanner())

        result = await planner.plan("关闭2号端口", self.context)

        self.assertEqual(result.command.action, "set_port_power")
        self.assertEqual(result.command.port, 2)
        self.assertFalse(result.command.enabled)
        self.assertEqual(result.model, "rules-fast-path")

    async def test_fast_path_handles_explicit_knowledge_without_model(
        self,
    ) -> None:
        planner = FastPathCommandPlanner(FailingPlanner())

        result = await planner.plan("PD 3.0 协议是什么？", self.context)

        self.assertEqual(result.command.action, "answer_knowledge")
        self.assertEqual(result.model, "rules-fast-path")

    async def test_fast_path_does_not_match_hi_inside_english_words(
        self,
    ) -> None:
        planner = FastPathCommandPlanner(FailingPlanner())

        with self.assertRaises(httpx.ConnectError):
            await planner.plan("which port is charging", self.context)

    async def test_model_knowledge_reason_is_normalized_with_route(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "unsupported",
                                "action": "unsupported_request",
                                "device_id": "model-device",
                                "need_confirmation": False,
                                "reason": "无关设备运维和固件知识",
                            }
                        )
                    }
                },
            )

        planner = OllamaCommandPlanner(
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(planner.aclose)

        result = await planner.plan("PD 3.0 是什么？", self.context)

        self.assertEqual(result.command.action, "answer_knowledge")
        self.assertEqual(result.command.reason, "查询固件与充电协议知识。")

    async def test_model_clarification_for_unrelated_request_is_scoped(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "clarify",
                                "action": "ask_clarification",
                                "device_id": "model-device",
                                "need_confirmation": False,
                                "reason": "信息不足。",
                            }
                        )
                    }
                },
            )

        planner = OllamaCommandPlanner(
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(planner.aclose)

        result = await planner.plan(
            "给我推荐一道晚餐菜谱",
            self.context,
        )

        self.assertEqual(result.command.intent, "unsupported")
        self.assertEqual(result.command.action, "unsupported_request")

    async def test_whole_device_query_gets_default_overview_focus(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "query_device",
                                "action": "get_status",
                                "device_id": "model-device",
                                "need_confirmation": False,
                                "reason": "查询在线状态。",
                                "response_focus": "auto",
                            }
                        )
                    }
                },
            )

        planner = OllamaCommandPlanner(
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(planner.aclose)

        result = await planner.plan("设备在线吗", self.context)

        self.assertEqual(
            result.command.response_focus,
            "device_overview",
        )


if __name__ == "__main__":
    unittest.main()

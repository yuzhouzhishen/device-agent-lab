from __future__ import annotations

import unittest

from langgraph.types import Command

from device_agent_lab.agent_contracts import DeviceAgentContext, DeviceCommand
from device_agent_lab.device_gateway import (
    DeviceGatewayError,
    MockDeviceGateway,
)
from device_agent_lab.knowledge_gateway import (
    GeneralKnowledgeResult,
    KnowledgeAnswer,
    KnowledgeCitation,
    KnowledgeGatewayError,
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


class RecordingKnowledgeGateway:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def answer(
        self,
        query: str,
        *,
        top_k: int = 4,
    ) -> KnowledgeAnswer:
        self.queries.append(query)
        return KnowledgeAnswer(
            status="answered",
            answer="先检查线缆和 PD 协商，再进行交叉验证。【1】",
            citations=[
                KnowledgeCitation(
                    id="firmware-guide",
                    title="端口不充电排查",
                    section="排查步骤",
                    source_url="private://firmware-guide",
                )
            ][:top_k],
            trace=["retrieve", "rerank", "generate"],
            retriever="hybrid",
            reranker="rrf",
            generation_mode="extractive",
        )


class UnavailableKnowledgeGateway:
    async def answer(
        self,
        query: str,
        *,
        top_k: int = 4,
    ) -> KnowledgeAnswer:
        del query, top_k
        raise KnowledgeGatewayError("service unavailable")


class NoEvidenceKnowledgeGateway:
    async def answer(
        self,
        query: str,
        *,
        top_k: int = 4,
    ) -> KnowledgeAnswer:
        del query, top_k
        return KnowledgeAnswer(
            status="no_evidence",
            answer="当前知识库没有找到足够证据，暂不回答。",
            generation_mode="none",
            trace=["retrieve", "grade_evidence", "refuse"],
        )


class RecordingGeneralKnowledgeProvider:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def answer(
        self,
        query: str,
    ) -> GeneralKnowledgeResult | None:
        self.queries.append(query)
        return GeneralKnowledgeResult(
            answer="PD 3.0 是 USB Power Delivery 的协议版本。",
            model="test-general-model",
        )


class CapabilityMismatchGateway(MockDeviceGateway):
    async def get_port_pd_status(self) -> dict:
        return {
            "ports": [
                {
                    "port": 1,
                    "pd_revision": "PD 3.0",
                    "request_capability_mismatch": True,
                }
            ]
        }


class EventuallyConsistentGateway(MockDeviceGateway):
    def __init__(self, controller: DeviceController) -> None:
        super().__init__(controller)
        self._stale_reads = 0
        self._stale_port: dict | None = None

    async def get_port_status(self, port_id: int) -> dict:
        if self._stale_reads and self._stale_port is not None:
            self._stale_reads -= 1
            return dict(self._stale_port)
        return await super().get_port_status(port_id)

    async def set_port_power(
        self,
        port_id: int,
        *,
        enabled: bool,
        power_w: float | None = None,
    ) -> dict:
        self._stale_port = await super().get_port_status(port_id)
        result = await super().set_port_power(
            port_id,
            enabled=enabled,
            power_w=power_w,
        )
        self._stale_reads = 2
        return result


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
            ["validate", "query", "summarize"],
        )
        self.assertNotIn("knowledge", result)

    async def test_chat_answers_without_calling_device_or_knowledge(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="chat",
            action="respond_chat",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="回答身份问题。",
            reply="我是 DeviceOps。",
        )

        result = await graph.ainvoke(
            {
                "request": "你是谁",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-chat"}},
        )

        self.assertEqual(result["summary"], "我是 DeviceOps。")
        self.assertEqual(
            result["trace"],
            ["validate", "respond_chat", "summarize"],
        )
        self.assertEqual(result["result"]["response_kind"], "conversation")

    async def test_direct_knowledge_question_uses_rag_only(self) -> None:
        knowledge = RecordingKnowledgeGateway()
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=knowledge,
        )
        command = DeviceCommand(
            intent="knowledge_query",
            action="answer_knowledge",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="查询固件知识。",
        )

        result = await graph.ainvoke(
            {
                "request": "PD 3.0 协议是什么？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-knowledge"}},
        )

        self.assertEqual(knowledge.queries, ["PD 3.0 协议是什么？"])
        self.assertIn("检查线缆", result["summary"])
        self.assertEqual(result["knowledge"][0]["id"], "firmware-guide")
        self.assertEqual(
            result["trace"],
            ["validate", "answer_knowledge", "summarize"],
        )

    async def test_public_concept_uses_labeled_general_fallback_after_rag_miss(
        self,
    ) -> None:
        general = RecordingGeneralKnowledgeProvider()
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=NoEvidenceKnowledgeGateway(),
            general_knowledge_provider=general,
        )
        command = DeviceCommand(
            intent="knowledge_query",
            action="answer_knowledge",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="查询固件与充电协议知识。",
        )

        result = await graph.ainvoke(
            {
                "request": "PD 3.0 是什么？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-general-knowledge"}},
        )

        self.assertEqual(general.queries, ["PD 3.0 是什么？"])
        self.assertEqual(
            result["result"]["response_kind"],
            "general_knowledge",
        )
        self.assertEqual(
            result["knowledge_metadata"]["origin"],
            "general_model",
        )
        self.assertEqual(result["knowledge"], [])
        self.assertIn("未经当前知识库验证", result["warnings"][0])
        self.assertIn("USB Power Delivery", result["summary"])

    async def test_specific_device_fact_does_not_use_general_fallback(
        self,
    ) -> None:
        general = RecordingGeneralKnowledgeProvider()
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=NoEvidenceKnowledgeGateway(),
            general_knowledge_provider=general,
        )
        command = DeviceCommand(
            intent="knowledge_query",
            action="answer_knowledge",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="查询具体设备能力。",
        )

        result = await graph.ainvoke(
            {
                "request": "我们的设备支持 PD 3.0 吗？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-private-fact"}},
        )

        self.assertEqual(general.queries, [])
        self.assertEqual(result["knowledge_metadata"]["status"], "no_evidence")
        self.assertIn("没有找到足够证据", result["summary"])

    async def test_specific_device_fact_rejects_related_but_indirect_rag_hits(
        self,
    ) -> None:
        general = RecordingGeneralKnowledgeProvider()
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=RecordingKnowledgeGateway(),
            general_knowledge_provider=general,
        )
        command = DeviceCommand(
            intent="knowledge_query",
            action="answer_knowledge",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="查询具体设备能力。",
        )

        result = await graph.ainvoke(
            {
                "request": "我们的设备支持 PD 3.0 吗？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-indirect-evidence"}},
        )

        self.assertEqual(general.queries, [])
        self.assertEqual(result["knowledge"], [])
        self.assertEqual(result["knowledge_metadata"]["status"], "no_evidence")
        self.assertIn("直接证明", result["summary"])
        self.assertIn(
            "specific_capability_evidence_guard",
            result["knowledge_metadata"]["trace"],
        )

    async def test_clarification_is_not_reported_as_device_failure(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="clarify",
            action="ask_clarification",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="请说明要控制哪个端口。",
        )

        result = await graph.ainvoke(
            {
                "request": "帮我关闭一个端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-clarify"}},
        )

        self.assertTrue(result["result"]["ok"])
        self.assertTrue(result["result"]["needs_clarification"])
        self.assertEqual(result["summary"], "请说明要控制哪个端口。")
        self.assertNotIn("设备操作失败", result["summary"])

    async def test_previous_result_explanation_uses_saved_evidence(self) -> None:
        graph = build_ops_workflow(self.gateway)
        command = DeviceCommand(
            intent="chat",
            action="explain_previous",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="解释上一条结果。",
        )

        result = await graph.ainvoke(
            {
                "request": "刚才为什么这么判断？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "conversation": {
                    "recent_turns": [
                        {
                            "request": "诊断3号端口",
                            "summary": "3号端口存在 PD 能力不匹配。",
                            "action": "diagnose_port",
                            "port": 3,
                            "intent": "query_device",
                            "evidence": ["PD 请求能力不匹配"],
                            "knowledge_titles": ["充电问题排查"],
                        }
                    ]
                },
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-explain"}},
        )

        self.assertIn("PD 请求能力不匹配", result["summary"])
        self.assertIn("端口诊断", result["summary"])
        self.assertIn("充电问题排查", result["summary"])
        self.assertEqual(result["result"]["response_kind"], "explanation")

    async def test_diagnosis_retrieves_guidance_after_real_evidence(self) -> None:
        knowledge = RecordingKnowledgeGateway()
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=knowledge,
        )
        command = DeviceCommand(
            intent="query_device",
            action="diagnose_port",
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

        self.assertEqual(result["knowledge"][0]["id"], "firmware-guide")
        self.assertIn("知识库建议", result["summary"])
        self.assertIn("未检测到连接设备", knowledge.queries[0])
        self.assertNotIn("2号端口", knowledge.queries[0])

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
                "collect_port_status",
                "collect_charging_status",
                "collect_pd_status",
                "collect_temperature_mode",
                "analyze_diagnosis",
                "retrieve_knowledge",
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

    async def test_pd_capability_mismatch_is_not_reported_as_healthy(
        self,
    ) -> None:
        graph = build_ops_workflow(
            CapabilityMismatchGateway(self.controller)
        )
        command = DeviceCommand(
            intent="query_device",
            action="diagnose_port",
            device_id=self.context.device_id,
            port=1,
            need_confirmation=False,
            reason="诊断1号端口功率异常。",
        )

        result = await graph.ainvoke(
            {
                "request": "1号端口功率为什么不符合预期？",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-pd-mismatch"}},
        )

        diagnosis = result["result"]["diagnosis"]
        self.assertEqual(diagnosis["health"], "attention")
        self.assertIn("能力不匹配", diagnosis["conclusion"])

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
            ["validate", "prepare_control", "summarize"],
        )

    async def test_whole_device_diagnosis_collects_evidence_and_rag(
        self,
    ) -> None:
        knowledge = RecordingKnowledgeGateway()
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=knowledge,
        )
        command = DeviceCommand(
            intent="query_device",
            action="diagnose_device",
            device_id=self.context.device_id,
            need_confirmation=False,
            reason="检查整台设备是否异常。",
        )

        result = await graph.ainvoke(
            {
                "request": "帮我检查设备当前有没有异常",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-device-diagnose"}},
        )

        self.assertEqual(result["result"]["action"], "diagnose_device")
        health = result["result"]["diagnosis"]["health"]
        self.assertIn(
            health,
            {"healthy", "attention", "partial"},
        )
        self.assertEqual(result["knowledge"][0]["id"], "firmware-guide")
        if health == "healthy":
            self.assertIn(
                "检索主题：充电设备固件验收",
                knowledge.queries[0],
            )
            self.assertNotIn("实时证据：", knowledge.queries[0])
        else:
            self.assertIn("实时证据：", knowledge.queries[0])
        self.assertEqual(
            result["trace"],
            [
                "validate",
                "collect_device_status",
                "collect_charging_status",
                "collect_pd_status",
                "collect_temperature_mode",
                "analyze_device_diagnosis",
                "retrieve_knowledge",
                "summarize",
            ],
        )

    async def test_diagnosis_degrades_when_rag_is_unavailable(self) -> None:
        graph = build_ops_workflow(
            self.gateway,
            knowledge_gateway=UnavailableKnowledgeGateway(),
        )
        command = DeviceCommand(
            intent="query_device",
            action="diagnose_port",
            device_id=self.context.device_id,
            port=2,
            need_confirmation=False,
            reason="诊断2号端口。",
        )

        result = await graph.ainvoke(
            {
                "request": "诊断2号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config={"configurable": {"thread_id": "ops-rag-unavailable"}},
        )

        self.assertTrue(result["result"]["ok"])
        self.assertEqual(
            result["knowledge_metadata"]["status"],
            "unavailable",
        )
        self.assertIn("仅依据实时设备证据", result["warnings"][0])
        self.assertIn("未检测到连接设备", result["summary"])

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

    async def test_control_verification_polls_until_telemetry_converges(
        self,
    ) -> None:
        gateway = EventuallyConsistentGateway(self.controller)
        graph = build_ops_workflow(
            gateway,
            control_verify_attempts=3,
        )
        command = DeviceCommand(
            intent="control_device",
            action="set_port_power",
            device_id=self.context.device_id,
            port=3,
            enabled=True,
            need_confirmation=True,
            reason="打开3号端口。",
        )
        config = {"configurable": {"thread_id": "ops-eventual-control"}}

        await graph.ainvoke(
            {
                "request": "打开3号端口",
                "command": command.model_dump(),
                "context": self.context.model_dump(),
                "trace": [],
            },
            config=config,
        )
        completed = await graph.ainvoke(
            Command(resume=True),
            config=config,
        )

        self.assertEqual(
            completed["result"]["verification"]["status"],
            "verified",
        )
        self.assertEqual(
            completed["result"]["verification"]["attempts"],
            3,
        )


if __name__ == "__main__":
    unittest.main()

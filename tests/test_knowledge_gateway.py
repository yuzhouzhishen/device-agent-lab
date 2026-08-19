from __future__ import annotations

import json
import unittest

import httpx

from device_agent_lab.knowledge_gateway import (
    HttpKnowledgeGateway,
    KnowledgeCitation,
    KnowledgeGatewayError,
    OllamaGeneralKnowledgeProvider,
    citations_support_specific_request,
    is_public_general_knowledge_request,
)


class HttpKnowledgeGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_maps_standalone_rag_response(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "status": "answered",
                    "answer": "检查 PD 协商和线缆能力。【1】",
                    "citations": [
                        {
                            "source_id": "wiki-42",
                            "title": "PD 协商排查",
                            "section": "操作步骤",
                            "source_url": "private://wiki-42",
                        }
                    ],
                    "trace": ["rewrite", "retrieve", "rerank", "generate"],
                    "retriever": "hybrid",
                    "reranker": "rrf",
                    "generation_mode": "ollama",
                    "degraded": False,
                    "latency_ms": 82.4,
                },
            )

        gateway = HttpKnowledgeGateway(
            "http://rag.test",
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(gateway.aclose)

        answer = await gateway.answer("3号端口功率异常", top_k=3)

        self.assertEqual(captured, {"query": "3号端口功率异常", "top_k": 3})
        self.assertEqual(answer.status, "answered")
        self.assertEqual(answer.citations[0].id, "wiki-42")
        self.assertEqual(answer.retriever, "hybrid")
        self.assertEqual(answer.generation_mode, "ollama")

    async def test_http_failure_has_an_explicit_domain_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(503, json={"detail": "starting"})

        gateway = HttpKnowledgeGateway(
            "http://rag.test",
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(gateway.aclose)

        with self.assertRaisesRegex(
            KnowledgeGatewayError,
            "unavailable",
        ):
            await gateway.answer("设备异常")


class GeneralKnowledgeFallbackTest(unittest.IsolatedAsyncioTestCase):
    def test_allows_public_domain_concepts_only(self) -> None:
        self.assertTrue(
            is_public_general_knowledge_request("PD 3.0 是什么？")
        )
        self.assertTrue(
            is_public_general_knowledge_request("MQTT 和 HTTP 有什么区别？")
        )
        self.assertFalse(
            is_public_general_knowledge_request("我们的设备支持 PD 3.0 吗？")
        )
        self.assertFalse(
            is_public_general_knowledge_request("今天天气怎么样？")
        )

    def test_specific_capability_requires_matching_citation_metadata(
        self,
    ) -> None:
        unrelated = [
            KnowledgeCitation(
                id="pd-policy",
                title="充电问题排查",
                section="PD Policy Engine 状态机",
            )
        ]
        direct = [
            KnowledgeCitation(
                id="pd30-capability",
                title="设备 PD 3.0 能力说明",
                section="支持范围",
            )
        ]

        self.assertFalse(
            citations_support_specific_request(
                "我们的设备支持 PD 3.0 吗？",
                unrelated,
            )
        )
        self.assertTrue(
            citations_support_specific_request(
                "我们的设备支持 PD 3.0 吗？",
                direct,
            )
        )

    async def test_ollama_provider_maps_structured_answer(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "answerable": True,
                                "answer": "PD 3.0 是 USB PD 的一个协议版本。",
                                "reason": "公开协议概念",
                            }
                        )
                    }
                },
            )

        provider = OllamaGeneralKnowledgeProvider(
            transport=httpx.MockTransport(handler)
        )
        self.addAsyncCleanup(provider.aclose)

        answer = await provider.answer("PD 3.0 是什么？")

        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertIn("USB PD", answer.answer)
        self.assertEqual(answer.model, "llama3.1:8b")
        self.assertEqual(captured["options"], {"temperature": 0})

    async def test_ollama_provider_can_decline_specific_facts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "answerable": False,
                                "answer": "",
                                "reason": "需要设备证据",
                            }
                        )
                    }
                },
            )

        provider = OllamaGeneralKnowledgeProvider(
            transport=httpx.MockTransport(handler)
        )
        self.addAsyncCleanup(provider.aclose)

        self.assertIsNone(await provider.answer("这台设备支持 PD 3.0 吗？"))

    async def test_pps_is_scoped_to_usb_power_delivery(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "answerable": True,
                                "answer": (
                                    "PPS 是 USB PD 的 Programmable Power "
                                    "Supply，可动态调节电压和电流。"
                                ),
                                "reason": "公开协议概念",
                            }
                        )
                    }
                },
            )

        provider = OllamaGeneralKnowledgeProvider(
            transport=httpx.MockTransport(handler)
        )
        self.addAsyncCleanup(provider.aclose)

        answer = await provider.answer("PPS 是什么？")

        self.assertIsNotNone(answer)
        messages = captured["messages"]
        assert isinstance(messages, list)
        self.assertIn("USB Power Delivery", messages[-1]["content"])

    async def test_pps_rejects_a_wrong_communications_definition(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "answerable": True,
                                "answer": "PPS 是 Pulse Position Modulation。",
                                "reason": "公开概念",
                            }
                        )
                    }
                },
            )

        provider = OllamaGeneralKnowledgeProvider(
            transport=httpx.MockTransport(handler)
        )
        self.addAsyncCleanup(provider.aclose)

        self.assertIsNone(await provider.answer("PPS 是什么？"))


if __name__ == "__main__":
    unittest.main()

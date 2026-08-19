from __future__ import annotations

import json
import re
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from device_agent_lab.knowledge_base import LocalKnowledgeBase


class KnowledgeGatewayError(RuntimeError):
    pass


class KnowledgeCitation(BaseModel):
    id: str
    title: str
    section: str = ""
    source_url: str = ""
    content: str | None = None


class KnowledgeAnswer(BaseModel):
    status: Literal["answered", "no_evidence"]
    answer: str
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list)
    retriever: str = "local"
    reranker: str = "none"
    generation_mode: str = "extractive"
    degraded: bool = False
    latency_ms: float = 0.0


class GeneralKnowledgeResult(BaseModel):
    answer: str
    model: str


class GeneralKnowledgeResponse(BaseModel):
    answerable: bool
    answer: str = ""
    reason: str = ""


class KnowledgeGateway(Protocol):
    async def answer(
        self,
        query: str,
        *,
        top_k: int = 4,
    ) -> KnowledgeAnswer: ...


class GeneralKnowledgeProvider(Protocol):
    async def answer(
        self,
        query: str,
    ) -> GeneralKnowledgeResult | None: ...


class OllamaGeneralKnowledgeProvider:
    """Restricted fallback for public embedded and charging concepts."""

    def __init__(
        self,
        *,
        model_name: str = "llama3.1:8b",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model_name = model_name
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def answer(
        self,
        query: str,
    ) -> GeneralKnowledgeResult | None:
        response = await self._client.post(
            "/api/chat",
            json={
                "model": self._model_name,
                "stream": False,
                "format": GeneralKnowledgeResponse.model_json_schema(),
                "options": {"temperature": 0},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是公开通用的嵌入式固件与 USB 充电协议知识助手。"
                            "默认语境是 USB Power Delivery；PPS 表示"
                            " Programmable Power Supply（可编程电源），"
                            "不是通信调制中的 Pulse Position Modulation。"
                            "只回答公开、稳定的概念和原理；不得推断任何"
                            "具体设备、公司内部实现、实时状态、支持能力或"
                            "配置值。若问题需要这些信息，answerable=false。"
                            "回答使用中文，准确、简洁，不伪造文档引用。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": _contextualize_general_query(query),
                    },
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("message", {}).get("content", "")
        raw = json.loads(content) if isinstance(content, str) else content
        parsed = GeneralKnowledgeResponse.model_validate(raw)
        if (
            not parsed.answerable
            or not parsed.answer.strip()
            or not _general_answer_matches_domain(query, parsed.answer)
        ):
            return None
        return GeneralKnowledgeResult(
            answer=parsed.answer.strip(),
            model=self._model_name,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _contextualize_general_query(query: str) -> str:
    if re.search(r"\bpps\b", query, flags=re.IGNORECASE):
        return (
            "请限定在 USB Power Delivery 充电协议语境下回答。"
            f"原问题：{query}"
        )
    return query


def _general_answer_matches_domain(query: str, answer: str) -> bool:
    if not re.search(r"\bpps\b", query, flags=re.IGNORECASE):
        return True
    normalized = answer.lower()
    if "pulse position" in normalized or "脉冲位置" in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "programmable power supply",
            "可编程电源",
            "可编程供电",
        )
    )


def is_public_general_knowledge_request(query: str) -> bool:
    """Allow only generic domain concepts to bypass a RAG miss."""
    normalized = query.strip().lower()
    if is_specific_device_knowledge_request(normalized):
        return False

    domain_terms = (
        "pps",
        "usb-c",
        "type-c",
        "mqtt",
        "ota",
        "wi-fi",
        "wifi",
        "蓝牙",
        "bootloader",
        "firmware",
        "固件",
        "状态机",
        "功率分配",
        "温控",
        "充电协议",
    )
    has_domain = any(term in normalized for term in domain_terms) or bool(
        re.search(r"\bpd\s*(?:[0-9](?:\.[0-9])?)?\b", normalized)
    )
    if not has_domain:
        return False

    general_question_cues = (
        "是什么",
        "什么意思",
        "介绍",
        "概念",
        "区别",
        "原理",
        "作用",
        "如何工作",
        "怎么工作",
        "有哪些能力",
        "支持哪些",
        "what is",
        "difference",
        "how does",
    )
    return any(cue in normalized for cue in general_question_cues)


def is_specific_device_knowledge_request(query: str) -> bool:
    normalized = query.strip().lower()
    private_or_device_cues = (
        "当前",
        "现在",
        "这台",
        "本机",
        "我们的",
        "公司",
        "内部",
        "具体设备",
        "设备是否",
        "设备支持",
        "型号",
        "序列号",
        "psn",
        "xdp",
        "cp-02",
        "cp02",
        "remote pd phy",
        "esp32 remote",
        "sw356",
        "固件版本",
        "配置值",
    )
    return any(cue in normalized for cue in private_or_device_cues)


def citations_support_specific_request(
    query: str,
    citations: list[KnowledgeCitation],
) -> bool:
    """Require citation metadata to name the requested capability/version."""
    normalized_query = _compact_technical_text(query)
    patterns = (
        r"pd[0-9](?:\.[0-9])?",
        r"pps",
        r"usbc",
        r"typec",
        r"mqtt",
        r"ota",
        r"wifi",
        r"bluetooth",
        r"bootloader",
    )
    requested_terms = {
        match.group(0)
        for pattern in patterns
        for match in re.finditer(pattern, normalized_query)
    }
    if not requested_terms:
        return False
    citation_text = _compact_technical_text(
        " ".join(
            f"{citation.title} {citation.section}"
            for citation in citations
        )
    )
    return all(term in citation_text for term in requested_terms)


def _compact_technical_text(value: str) -> str:
    normalized = value.strip().lower().replace("／", "/")
    return re.sub(r"[\s_-]+", "", normalized)


class LocalKnowledgeGateway:
    """Small offline fallback used when the standalone RAG service is absent."""

    def __init__(
        self,
        knowledge_base: LocalKnowledgeBase | None = None,
    ) -> None:
        self._knowledge = knowledge_base or LocalKnowledgeBase()

    async def answer(
        self,
        query: str,
        *,
        top_k: int = 4,
    ) -> KnowledgeAnswer:
        documents = self._knowledge.search(query, limit=top_k)
        if not documents:
            return KnowledgeAnswer(
                status="no_evidence",
                answer="本地知识库没有找到足够证据。",
                generation_mode="none",
            )
        citations = [
            KnowledgeCitation(
                id=str(document["id"]),
                title=str(document["title"]),
                section="local-playbook",
                source_url=f"local://{document['id']}",
                content=str(document["content"]),
            )
            for document in documents
        ]
        return KnowledgeAnswer(
            status="answered",
            answer="\n".join(
                f"【{index}】{citation.content}"
                for index, citation in enumerate(citations[:2], start=1)
            ),
            citations=citations,
            trace=["local_retrieve", "extractive_answer"],
        )


class HttpKnowledgeGateway:
    """Client for the independently deployable Firmware Knowledge Agent."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def answer(
        self,
        query: str,
        *,
        top_k: int = 4,
    ) -> KnowledgeAnswer:
        try:
            response = await self._client.post(
                "/v1/agent/answer",
                json={"query": query, "top_k": top_k},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise KnowledgeGatewayError(
                "Firmware Knowledge Agent unavailable"
            ) from exc
        return KnowledgeAnswer(
            status=payload["status"],
            answer=str(payload.get("answer", "")),
            citations=[
                KnowledgeCitation(
                    id=str(citation["source_id"]),
                    title=str(citation["title"]),
                    section=str(citation.get("section", "")),
                    source_url=str(citation.get("source_url", "")),
                )
                for citation in payload.get("citations", [])
            ],
            trace=[str(step) for step in payload.get("trace", [])],
            retriever=str(payload.get("retriever", "unknown")),
            reranker=str(payload.get("reranker", "none")),
            generation_mode=str(
                payload.get("generation_mode", "unknown")
            ),
            degraded=bool(payload.get("degraded", False)),
            latency_ms=float(payload.get("latency_ms", 0.0)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

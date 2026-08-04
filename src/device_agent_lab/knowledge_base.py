from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class KnowledgeDocument(BaseModel):
    id: str
    title: str
    keywords: list[str] = Field(default_factory=list)
    content: str


class LocalKnowledgeBase:
    """Small deterministic retriever for local device operation guides."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or Path(__file__).with_name("knowledge")
        self._documents = self._load_documents()

    def search(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        normalized_query = _normalize(query)
        query_terms = _terms(normalized_query)
        ranked: list[tuple[int, KnowledgeDocument]] = []
        for document in self._documents:
            keyword_score = 0
            for keyword in document.keywords:
                normalized_keyword = _normalize(keyword)
                if normalized_keyword and normalized_keyword in normalized_query:
                    keyword_score += 10
            searchable = _normalize(
                " ".join([document.title, *document.keywords, document.content])
            )
            overlap_score = len(query_terms & _terms(searchable))
            score = keyword_score + overlap_score
            if keyword_score > 0 or overlap_score >= 4:
                ranked.append((score, document))

        ranked.sort(key=lambda item: (-item[0], item[1].id))
        return [
            document.model_dump()
            for _, document in ranked[: max(0, limit)]
        ]

    def _load_documents(self) -> list[KnowledgeDocument]:
        if not self._directory.exists():
            return []
        documents = []
        for path in sorted(self._directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            documents.append(KnowledgeDocument.model_validate(data))
        return documents


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value.lower())


def _terms(value: str) -> set[str]:
    ascii_terms = set(re.findall(r"[a-z0-9_+-]{2,}", value))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", value))
    chinese_terms = {
        chinese[index : index + 2]
        for index in range(max(0, len(chinese) - 1))
    }
    return ascii_terms | chinese_terms

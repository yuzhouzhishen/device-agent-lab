"""Latency and token accounting for one device operations run."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """Token counts reported by the planning model for a single call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_messages(cls, messages: Any) -> TokenUsage | None:
        """Sum `usage_metadata` across LangChain messages, if any carry it."""
        if not isinstance(messages, (list, tuple)):
            return None
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        found = False
        for message in messages:
            metadata = getattr(message, "usage_metadata", None)
            if not isinstance(metadata, Mapping):
                continue
            found = True
            input_tokens += _as_int(metadata.get("input_tokens"))
            output_tokens += _as_int(metadata.get("output_tokens"))
            total_tokens += _as_int(metadata.get("total_tokens"))
        if not found:
            return None
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens or input_tokens + output_tokens,
        )


class TokenPricing(BaseModel):
    """Operator-supplied unit prices, quoted per one million tokens.

    Prices are deliberately not hard-coded: vendor rates change, and a stale
    constant would quietly report wrong costs. When unset, runs report token
    counts and leave `cost` empty rather than guessing.
    """

    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    currency: str = "CNY"

    def cost_of(self, usage: TokenUsage) -> float:
        millions_in = usage.input_tokens / 1_000_000
        millions_out = usage.output_tokens / 1_000_000
        return round(
            millions_in * self.input_per_1m
            + millions_out * self.output_per_1m,
            6,
        )


class StepTiming(BaseModel):
    """Wall-clock time attributed to one workflow node."""

    node: str
    duration_ms: float


class RunMetrics(BaseModel):
    """What a single run cost in time and tokens."""

    duration_ms: float = 0.0
    planner_ms: float = 0.0
    workflow_ms: float = 0.0
    steps: list[StepTiming] = Field(default_factory=list)
    usage: TokenUsage | None = None
    cost: float | None = None
    currency: str | None = None
    model: str | None = None


def merge_run_metrics(
    first: RunMetrics | None,
    second: RunMetrics | None,
) -> RunMetrics | None:
    """Combine the planning and resumed-control segments of one request."""
    if first is None:
        return second
    if second is None:
        return first
    usage = _merge_usage(first.usage, second.usage)
    costs = [
        value for value in (first.cost, second.cost) if value is not None
    ]
    return RunMetrics(
        duration_ms=round(first.duration_ms + second.duration_ms, 2),
        planner_ms=round(first.planner_ms + second.planner_ms, 2),
        workflow_ms=round(first.workflow_ms + second.workflow_ms, 2),
        steps=[*first.steps, *second.steps],
        usage=usage,
        cost=round(sum(costs), 6) if costs else None,
        currency=first.currency or second.currency,
        model=first.model or second.model,
    )


def _merge_usage(
    first: TokenUsage | None,
    second: TokenUsage | None,
) -> TokenUsage | None:
    if first is None:
        return second
    if second is None:
        return first
    return TokenUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
    )


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

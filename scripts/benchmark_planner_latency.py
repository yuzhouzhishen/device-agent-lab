#!/usr/bin/env python3
"""Compare direct Ollama planning with the confidence-gated fast path."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.planner import (
    CommandPlanner,
    FastPathCommandPlanner,
    OllamaCommandPlanner,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "benchmarks" / "planner_latency_cases.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "benchmarks" / "reports" / "planner_latency_ollama.json"
)


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


async def benchmark_strategy(
    *,
    name: str,
    planner: CommandPlanner,
    cases: list[dict[str, str]],
    context: DeviceAgentContext,
    repeats: int,
) -> dict[str, Any]:
    measurements: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        for case in cases:
            started = perf_counter()
            result = await planner.plan(case["request"], context)
            duration_ms = round((perf_counter() - started) * 1000, 2)
            action = result.command.action
            measurements.append(
                {
                    "case_id": case["id"],
                    "repeat": repeat,
                    "duration_ms": duration_ms,
                    "expected_action": case["expected_action"],
                    "actual_action": action,
                    "correct": action == case["expected_action"],
                    "planner_model": result.model,
                }
            )

    durations = [item["duration_ms"] for item in measurements]
    correct = sum(1 for item in measurements if item["correct"])
    return {
        "strategy": name,
        "request_count": len(measurements),
        "correct_count": correct,
        "accuracy": round(correct / len(measurements), 4),
        "average_ms": round(mean(durations), 2),
        "p50_ms": round(median(durations), 2),
        "p95_ms": round(percentile(durations, 0.95), 2),
        "measurements": measurements,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    context = DeviceAgentContext(
        device_id="BENCHMARK-DEVICE",
        online=True,
        allowed_ports=[1, 2, 3],
        operator_role="benchmark",
    )

    direct = OllamaCommandPlanner(
        model_name=args.model,
        base_url=args.ollama_url,
        timeout_seconds=args.timeout,
    )
    fast_primary = OllamaCommandPlanner(
        model_name=args.model,
        base_url=args.ollama_url,
        timeout_seconds=args.timeout,
    )
    fast = FastPathCommandPlanner(fast_primary)
    try:
        if not args.skip_warmup:
            await direct.plan(cases[0]["request"], context)
        direct_report = await benchmark_strategy(
            name="direct-ollama",
            planner=direct,
            cases=cases,
            context=context,
            repeats=args.repeats,
        )
        fast_report = await benchmark_strategy(
            name="confidence-gated-fast-path",
            planner=fast,
            cases=cases,
            context=context,
            repeats=args.repeats,
        )
    finally:
        await direct.aclose()
        await fast.aclose()

    speedup = direct_report["p50_ms"] / max(fast_report["p50_ms"], 0.01)
    return {
        "schema_version": 1,
        "model": args.model,
        "ollama_url": args.ollama_url,
        "case_count": len(cases),
        "repeats": args.repeats,
        "warmup": not args.skip_warmup,
        "strategies": [direct_report, fast_report],
        "p50_speedup": round(speedup, 2),
        "evidence_boundary": [
            "Measures planner wall-clock latency on one local machine.",
            "Does not include device, RAG, network, or UI latency.",
            "Results should not be generalized to other hardware or models."
        ]
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    return args


def main() -> int:
    args = parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for strategy in report["strategies"]:
        print(
            f"{strategy['strategy']}: accuracy={strategy['accuracy']:.4f}, "
            f"p50={strategy['p50_ms']:.2f} ms, "
            f"p95={strategy['p95_ms']:.2f} ms"
        )
    print(f"p50 speedup: {report['p50_speedup']:.2f}x")
    print(f"Report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

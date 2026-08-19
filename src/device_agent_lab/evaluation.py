from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceConversationContext,
    validate_command_against_context,
)
from device_agent_lab.planner import (
    CommandPlanner,
    FastPathCommandPlanner,
    GeminiCommandPlanner,
    OllamaCommandPlanner,
    RuleBasedCommandPlanner,
)


class ExpectedCommand(BaseModel):
    intent: str | None = None
    action: str
    port: int | None
    enabled: bool | None
    need_confirmation: bool
    response_focus: str | None = None
    response_detail: str | None = None
    valid: bool = True


class PlannerEvaluationCase(BaseModel):
    id: str
    category: str
    request: str
    conversation: DeviceConversationContext = Field(
        default_factory=DeviceConversationContext
    )
    expected: ExpectedCommand


def load_evaluation_cases(path: Path) -> list[PlannerEvaluationCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("evaluation dataset must be a JSON array")
    return [
        PlannerEvaluationCase.model_validate(item)
        for item in payload
    ]


async def evaluate_planner(
    planner: CommandPlanner,
    context: DeviceAgentContext,
    cases: list[PlannerEvaluationCase],
    *,
    planner_name: str,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            command = (
                await planner.plan(
                    case.request,
                    context,
                    case.conversation,
                )
            ).command
            try:
                validate_command_against_context(command, context)
                actual_valid = True
                validation_error = None
            except ValueError as exc:
                actual_valid = False
                validation_error = str(exc)

            checks = {
                "intent": (
                    case.expected.intent is None
                    or command.intent == case.expected.intent
                ),
                "action": command.action == case.expected.action,
                "port": command.port == case.expected.port,
                "enabled": command.enabled == case.expected.enabled,
                "confirmation": (
                    command.need_confirmation
                    == case.expected.need_confirmation
                ),
                "validation": actual_valid == case.expected.valid,
                "response_focus": (
                    case.expected.response_focus is None
                    or command.response_focus == case.expected.response_focus
                ),
                "response_detail": (
                    case.expected.response_detail is None
                    or command.response_detail == case.expected.response_detail
                ),
            }
            results.append(
                {
                    "id": case.id,
                    "category": case.category,
                    "request": case.request,
                    "passed": all(checks.values()),
                    "checks": checks,
                    "expected": case.expected.model_dump(),
                    "actual": {
                        "intent": command.intent,
                        "action": command.action,
                        "port": command.port,
                        "enabled": command.enabled,
                        "need_confirmation": command.need_confirmation,
                        "response_focus": command.response_focus,
                        "response_detail": command.response_detail,
                        "valid": actual_valid,
                    },
                    "error": validation_error,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case.id,
                    "category": case.category,
                    "request": case.request,
                    "passed": False,
                    "checks": {
                        "intent": False,
                        "action": False,
                        "port": False,
                        "enabled": False,
                        "confirmation": False,
                        "validation": False,
                        "response_focus": False,
                        "response_detail": False,
                    },
                    "expected": case.expected.model_dump(),
                    "actual": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return _build_report(planner_name, results)


def write_evaluation_report(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _build_report(
    planner_name: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result["passed"])
    metric_names = (
        "intent",
        "action",
        "port",
        "enabled",
        "confirmation",
        "validation",
    )
    metrics = {
        f"{name}_accuracy": _rate(
            sum(
                1
                for result in results
                if result["checks"][name]
            ),
            total,
        )
        for name in metric_names
    }
    categories = sorted(
        {result["category"] for result in results}
    )
    category_metrics = {}
    for category in categories:
        category_results = [
            result
            for result in results
            if result["category"] == category
        ]
        category_metrics[category] = {
            "total": len(category_results),
            "passed": sum(
                1
                for result in category_results
                if result["passed"]
            ),
            "exact_match_rate": _rate(
                sum(
                    1
                    for result in category_results
                    if result["passed"]
                ),
                len(category_results),
            ),
        }

    return {
        "schema_version": 1,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "planner": planner_name,
        "total_cases": total,
        "passed_cases": passed,
        "exact_match_rate": _rate(passed, total),
        "metrics": metrics,
        "categories": category_metrics,
        "failures": [
            result
            for result in results
            if not result["passed"]
        ],
        "results": results,
    }


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate DeviceOps command planning without device calls."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evals/planner_cases.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evals/reports/planner_rules.json"),
    )
    parser.add_argument(
        "--planner",
        choices=("rules", "gemini", "ollama"),
        default="rules",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    load_dotenv()
    context = DeviceAgentContext(
        device_id=os.getenv("DEVICE_ID", "EVAL-DEVICE"),
        online=True,
        allowed_ports=[1, 2, 3, 4, 5],
        operator_role="evaluator",
    )
    if args.planner == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is required for --planner gemini"
            )
        planner: CommandPlanner = FastPathCommandPlanner(
            GeminiCommandPlanner(
                api_key=api_key,
                model_name=os.getenv(
                    "GEMINI_MODEL",
                    "gemini-2.5-flash",
                ),
            )
        )
    elif args.planner == "ollama":
        planner = FastPathCommandPlanner(
            OllamaCommandPlanner(
                model_name=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
                base_url=os.getenv(
                    "OLLAMA_BASE_URL",
                    "http://127.0.0.1:11434",
                ),
            )
        )
    else:
        planner = RuleBasedCommandPlanner()

    try:
        cases = load_evaluation_cases(args.dataset)
        report = await evaluate_planner(
            planner,
            context,
            cases,
            planner_name=args.planner,
        )
    finally:
        close = getattr(planner, "aclose", None)
        if close is not None:
            await close()
    write_evaluation_report(report, args.output)
    print(
        f"{args.planner} planner: "
        f"{report['passed_cases']}/{report['total_cases']} passed "
        f"(exact match {report['exact_match_rate']:.1%})"
    )
    print(f"report: {args.output}")
    return 0 if report["passed_cases"] == report["total_cases"] else 1


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Evaluate the public DeviceOps -> Firmware RAG demo end to end."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
from time import perf_counter
from typing import Any

import httpx
from fastapi.testclient import TestClient

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.api import create_app
from device_agent_lab.audit import JsonlAuditLog
from device_agent_lab.conversation_store import SqliteConversationStore
from device_agent_lab.device_gateway import MockDeviceGateway
from device_agent_lab.device_ops_service import DeviceOpsService
from device_agent_lab.knowledge_gateway import HttpKnowledgeGateway
from device_agent_lab.planner import RuleBasedCommandPlanner
from device_agent_lab.runtime import DeviceOpsRuntime, RuntimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAG_ROOT = PROJECT_ROOT.parent / "firmware-knowledge-agent"
DEFAULT_CASES = PROJECT_ROOT / "evals" / "mock_stack_e2e_cases.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "evals" / "reports" / "mock_stack_e2e.json"


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * quantile)))
    return ordered[index]


def _lookup(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def evaluate_request_payload(
    case: dict[str, Any],
    payload: dict[str, Any],
) -> list[str]:
    failures: list[str] = []

    expected_values = {
        "status": case.get("expected_status"),
        "command.action": case.get("expected_action"),
        "result.response_kind": case.get("expected_response_kind"),
        "knowledge_metadata.status": case.get("expected_knowledge_status"),
        "result.verification.status": case.get(
            "expected_verification_status"
        ),
    }
    for path, expected in expected_values.items():
        if expected is None:
            continue
        actual = _lookup(payload, path)
        if actual != expected:
            failures.append(f"{path}: expected {expected!r}, got {actual!r}")

    expected_mode = case.get("expected_result_port_mode")
    if expected_mode is not None:
        actual_mode = (
            _lookup(payload, "result.port.mode")
            or _lookup(payload, "result.after.data.mode")
        )
        if actual_mode != expected_mode:
            failures.append(
                "result port mode: "
                f"expected {expected_mode!r}, got {actual_mode!r}"
            )

    summary_fragment = case.get("expected_summary_contains")
    if summary_fragment and summary_fragment not in str(payload.get("summary", "")):
        failures.append(f"summary does not contain {summary_fragment!r}")

    trace = payload.get("trace", [])
    for step in case.get("expected_trace_steps", []):
        if step not in trace:
            failures.append(f"trace does not contain {step!r}")

    citation_source = case.get("expected_citation_source")
    if citation_source is not None:
        cited_sources = {
            str(item.get("id", ""))
            for item in payload.get("knowledge", [])
            if isinstance(item, dict)
        }
        if citation_source not in cited_sources:
            failures.append(
                f"citations do not contain source {citation_source!r}"
            )
    return failures


class RagBridge:
    def __init__(self, project_root: Path, log_path: Path) -> None:
        python = project_root / ".venv" / "bin" / "python"
        if not python.is_file():
            raise FileNotFoundError(
                f"Firmware RAG environment is missing {python}. "
                "Run `uv sync --extra dev` in that repository first."
            )
        catalog = project_root / "data" / "sample" / "sources.json"
        environment = {
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "FIRMWARE_RAG_CATALOG": str(catalog),
            "FIRMWARE_RAG_RETRIEVER": "bm25",
            "FIRMWARE_RAG_RERANKER": "none",
            "FIRMWARE_RAG_GENERATOR": "extractive",
        }
        self.log_path = log_path
        self._log = log_path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self.process = subprocess.Popen(
            [
                str(python),
                str(PROJECT_ROOT / "scripts" / "rag_api_bridge.py"),
                "--catalog",
                str(catalog),
            ],
            cwd=project_root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
        )

    def request(
        self,
        method: str,
        path: str,
        json_body: object | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        with self._lock:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Firmware RAG bridge exited with code "
                    f"{self.process.returncode}: {self._log_tail()}"
                )
            if self.process.stdin is None or self.process.stdout is None:
                raise RuntimeError("Firmware RAG bridge pipes are unavailable")
            self.process.stdin.write(
                json.dumps(
                    {"method": method, "path": path, "json": json_body},
                    ensure_ascii=False,
                )
                + "\n"
            )
            self.process.stdin.flush()
            ready, _, _ = select.select(
                [self.process.stdout],
                [],
                [],
                timeout_seconds,
            )
            if not ready:
                raise TimeoutError(
                    "Timed out waiting for Firmware RAG bridge response"
                )
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(
                    "Firmware RAG bridge returned no response: "
                    + self._log_tail()
                )
            return json.loads(line)

    def stop(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self._log.close()

    def _log_tail(self) -> str:
        self._log.flush()
        lines = self.log_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        return " | ".join(lines[-5:]) or "empty log"


class SubprocessRagTransport(httpx.AsyncBaseTransport):
    def __init__(self, bridge: RagBridge) -> None:
        self._bridge = bridge

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        content = await request.aread()
        json_body = json.loads(content) if content else None
        result = await asyncio.to_thread(
            self._bridge.request,
            request.method,
            request.url.raw_path.decode("ascii"),
            json_body,
        )
        return httpx.Response(
            int(result["status_code"]),
            headers=result.get("headers", {}),
            content=str(result.get("content", "")).encode("utf-8"),
            request=request,
        )


def build_device_runtime(
    *,
    bridge: RagBridge,
    runtime_directory: Path,
) -> DeviceOpsRuntime:
    settings = RuntimeSettings(
        backend="mock",
        planner_mode="rules",
        control_mode="automatic",
        knowledge_url="http://firmware-rag",
        general_knowledge_fallback=False,
        control_verify_delay_seconds=0.0,
        control_verify_attempts=2,
        audit_path=runtime_directory / "audit.jsonl",
        session_path=runtime_directory / "sessions.sqlite3",
    )
    context = DeviceAgentContext(
        device_id=settings.device_id,
        online=True,
        allowed_ports=list(settings.allowed_ports),
        operator_role=settings.operator_role,
    )
    gateway = MockDeviceGateway()
    planner = RuleBasedCommandPlanner()
    knowledge = HttpKnowledgeGateway(
        settings.knowledge_url or "http://firmware-rag",
        timeout_seconds=10.0,
        transport=SubprocessRagTransport(bridge),
    )
    service = DeviceOpsService(
        planner=planner,
        gateway=gateway,
        knowledge_gateway=knowledge,
        audit_log=JsonlAuditLog(settings.audit_path),
        conversation_store=SqliteConversationStore(settings.session_path),
        control_verify_delay_seconds=settings.control_verify_delay_seconds,
        control_verify_attempts=settings.control_verify_attempts,
    )
    return DeviceOpsRuntime(
        settings=settings,
        context=context,
        gateway=gateway,
        planner=planner,
        knowledge_gateway=knowledge,
        general_knowledge_provider=None,
        service=service,
    )


def run_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    rag_root = args.rag_project.expanduser().resolve()
    if not (rag_root / "pyproject.toml").is_file():
        raise FileNotFoundError(
            f"Firmware RAG project not found at {rag_root}. Clone it next to "
            "device-agent-lab or pass --rag-project."
        )
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation cases must be a non-empty JSON list")

    with tempfile.TemporaryDirectory(prefix="deviceops-e2e-") as temporary:
        runtime_directory = Path(temporary)
        rag = RagBridge(
            rag_root,
            runtime_directory / "firmware-rag.log",
        )
        try:
            health_response = rag.request(
                "GET",
                "/health",
                timeout_seconds=args.startup_timeout,
            )
            if int(health_response["status_code"]) != 200:
                raise RuntimeError(
                    "Firmware RAG health check failed: "
                    + str(health_response.get("content", ""))
                )
            rag_health = json.loads(str(health_response["content"]))
            runtime = build_device_runtime(
                bridge=rag,
                runtime_directory=runtime_directory,
            )
            results: list[dict[str, Any]] = []
            with TestClient(create_app(runtime)) as client:
                for case in cases:
                    started = perf_counter()
                    failures: list[str] = []
                    payload: dict[str, Any] = {}
                    try:
                        if case.get("kind") == "health":
                            device_health = client.get("/health")
                            device_health.raise_for_status()
                            payload = device_health.json()
                            if payload.get("backend") != "mock":
                                failures.append("DeviceOps backend is not mock")
                            if payload.get("knowledge") != "remote-rag":
                                failures.append(
                                    "DeviceOps knowledge gateway is not remote-rag"
                                )
                            if rag_health.get("sources") != 3:
                                failures.append("Firmware RAG did not load 3 sources")
                        elif case.get("kind") == "request":
                            response = client.post(
                                "/v1/requests",
                                json={
                                    "request": case["request"],
                                    "conversation_id": "mock-stack-e2e",
                                },
                            )
                            response.raise_for_status()
                            payload = response.json()
                            failures.extend(
                                evaluate_request_payload(case, payload)
                            )
                        else:
                            failures.append(
                                f"unknown case kind {case.get('kind')!r}"
                            )
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{type(exc).__name__}: {exc}")
                    duration_ms = round((perf_counter() - started) * 1000, 2)
                    results.append(
                        {
                            "id": str(case.get("id", "unknown")),
                            "passed": not failures,
                            "duration_ms": duration_ms,
                            "action": _lookup(payload, "command.action"),
                            "status": payload.get("status"),
                            "trace": payload.get("trace", []),
                            "citation_sources": sorted(
                                {
                                    str(item.get("id", ""))
                                    for item in payload.get("knowledge", [])
                                    if isinstance(item, dict)
                                }
                            ),
                            "failures": failures,
                        }
                    )
        finally:
            rag.stop()

    durations = [float(result["duration_ms"]) for result in results]
    passed = sum(1 for result in results if result["passed"])
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "public-mock-stack-e2e",
        "environment": {
            "device_backend": "mock",
            "planner": "rules",
            "control_mode": "automatic",
            "rag_transport": "subprocess-asgi-contract-bridge",
            "rag_retriever": rag_health.get("retriever"),
            "rag_reranker": rag_health.get("reranker"),
            "rag_generator": rag_health.get("generator"),
            "rag_sources": rag_health.get("sources"),
            "rag_chunks": rag_health.get("chunks"),
        },
        "summary": {
            "case_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4),
            "average_latency_ms": round(sum(durations) / len(durations), 2),
            "p95_latency_ms": round(percentile(durations, 0.95), 2),
        },
        "results": results,
        "evidence_boundary": [
            "Uses the public three-document sample corpus and an in-memory mock device.",
            "Exercises the real DeviceOps API, LangGraph workflow, HTTP knowledge gateway, and Firmware RAG API contract.",
            "Does not prove real-device, XDP MCP, private-corpus, Ollama, or production latency and reliability."
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the public DeviceOps and Firmware RAG stack."
    )
    parser.add_argument("--rag-project", type=Path, default=DEFAULT_RAG_ROOT)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_evaluation(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = report["summary"]
    print(
        f"mock-stack-e2e: {summary['passed']}/{summary['case_count']} passed, "
        f"p95={summary['p95_latency_ms']:.2f} ms"
    )
    print(f"Report: {args.output}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

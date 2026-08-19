#!/usr/bin/env python3
"""Run the public DeviceOps + Firmware RAG demo without external services."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import TextIO
from urllib.error import URLError
from urllib.request import urlopen


DEVICE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAG_ROOT = DEVICE_ROOT.parent / "firmware-knowledge-agent"


class DemoProcess:
    def __init__(
        self,
        *,
        name: str,
        project_root: Path,
        module: str,
        host: str,
        port: int,
        environment: dict[str, str],
        log_path: Path,
    ) -> None:
        self.name = name
        self.project_root = project_root
        self.health_url = f"http://{host}:{port}/health"
        self.log_path = log_path
        self._log: TextIO | None = None

        uvicorn = project_root / ".venv" / "bin" / "uvicorn"
        if not uvicorn.is_file():
            raise FileNotFoundError(
                f"{name} environment is missing {uvicorn}. Run `uv sync --extra dev` "
                "in that repository first."
            )

        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                str(uvicorn),
                module,
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=project_root,
            env=environment,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_until_ready(self, timeout_seconds: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout_seconds
        last_error = "service did not answer"
        while time.monotonic() < deadline:
            return_code = self.process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"{self.name} exited with code {return_code}. "
                    f"See {self.log_path}."
                )
            try:
                with urlopen(self.health_url, timeout=1) as response:
                    payload = json.load(response)
                if payload.get("status") == "ok":
                    return payload
                last_error = f"unexpected health response: {payload}"
            except (OSError, URLError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(0.2)
        raise TimeoutError(
            f"Timed out waiting for {self.name}: {last_error}. See {self.log_path}."
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None


def ensure_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise RuntimeError(
                    f"Cannot check port {host}:{port}: {exc}"
                ) from exc
            raise RuntimeError(
                f"Port {host}:{port} is already in use. Choose another port with "
                f"--device-port or --rag-port."
            ) from exc


def build_rag_environment(project_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "FIRMWARE_RAG_CATALOG": str(
                project_root / "data" / "sample" / "sources.json"
            ),
            "FIRMWARE_RAG_RETRIEVER": "bm25",
            "FIRMWARE_RAG_RERANKER": "none",
            "FIRMWARE_RAG_GENERATOR": "extractive",
        }
    )
    return environment


def build_device_environment(
    *,
    rag_url: str,
    runtime_directory: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    for private_name in (
        "DEVICE_PROFILES_FILE",
        "DEVICE_PROFILE_STATE",
        "XDP_MCP_URL",
    ):
        environment.pop(private_name, None)
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "DEVICE_BACKEND": "mock",
            "DEVICE_PLANNER": "rules",
            "DEVICE_PLANNER_FALLBACK": "true",
            "DEVICE_CONTROL_MODE": "automatic",
            "DEVICE_ALLOWED_PORTS": "1,2,3",
            "DEVICE_ID": "DEMO-CP02-001",
            "DEVICE_LABEL": "Mock 演示设备",
            "DEVICE_AUDIT_PATH": str(runtime_directory / "audit.jsonl"),
            "DEVICE_SESSION_DB": str(runtime_directory / "sessions.sqlite3"),
            "FIRMWARE_RAG_URL": rag_url,
            "GENERAL_KNOWLEDGE_FALLBACK": "false",
        }
    )
    return environment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the offline DeviceOps and Firmware RAG demo stack."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--device-port", type=int, default=8000)
    parser.add_argument("--rag-port", type=int, default=8011)
    parser.add_argument(
        "--rag-project",
        type=Path,
        default=DEFAULT_RAG_ROOT,
        help="Path to a sibling firmware-knowledge-agent checkout.",
    )
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rag_root = args.rag_project.expanduser().resolve()
    if not (rag_root / "pyproject.toml").is_file():
        raise FileNotFoundError(
            f"Firmware RAG project not found at {rag_root}. Clone it next to "
            "device-agent-lab or pass --rag-project."
        )

    ensure_port_available(args.host, args.rag_port)
    ensure_port_available(args.host, args.device_port)

    runtime_directory = DEVICE_ROOT / "var" / "mock-demo"
    processes: list[DemoProcess] = []
    try:
        rag = DemoProcess(
            name="Firmware Knowledge Agent",
            project_root=rag_root,
            module="firmware_knowledge_agent.api:app",
            host=args.host,
            port=args.rag_port,
            environment=build_rag_environment(rag_root),
            log_path=runtime_directory / "firmware-rag.log",
        )
        processes.append(rag)
        rag_health = rag.wait_until_ready(args.startup_timeout)

        rag_url = f"http://{args.host}:{args.rag_port}"
        device = DemoProcess(
            name="DeviceOps Agent",
            project_root=DEVICE_ROOT,
            module="device_agent_lab.api:app",
            host=args.host,
            port=args.device_port,
            environment=build_device_environment(
                rag_url=rag_url,
                runtime_directory=runtime_directory,
            ),
            log_path=runtime_directory / "deviceops.log",
        )
        processes.append(device)
        device_health = device.wait_until_ready(args.startup_timeout)

        print("Mock demo is ready.")
        print(f"  DeviceOps: http://{args.host}:{args.device_port}/")
        print(f"  Firmware RAG: {rag_url}/")
        print(f"  Logs: {runtime_directory}")
        print(
            "  Runtime: "
            f"DeviceOps={device_health.get('backend')}/rules, "
            f"RAG={rag_health.get('retriever')}/extractive"
        )
        print("Press Ctrl+C to stop both services.")

        while True:
            for service in processes:
                return_code = service.process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"{service.name} exited with code {return_code}. "
                        f"See {service.log_path}."
                    )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping mock demo...")
        return 0
    finally:
        for service in reversed(processes):
            service.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

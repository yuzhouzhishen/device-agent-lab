from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from device_agent_lab.api import create_app
from device_agent_lab.device_profiles import (
    DeviceProfile,
    DeviceProfileCatalog,
    DeviceRuntimeCoordinator,
)
from device_agent_lab.runtime import RuntimeSettings, create_runtime


class DeviceOpsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        audit_path = Path(temporary_directory.name) / "audit.jsonl"
        session_path = Path(temporary_directory.name) / "sessions.sqlite3"
        runtime = asyncio.run(
            create_runtime(
                RuntimeSettings.from_mapping(
                    {
                        "DEVICE_AUDIT_PATH": str(audit_path),
                        "DEVICE_SESSION_DB": str(session_path),
                    }
                )
            )
        )
        self.client = TestClient(create_app(runtime))

    def test_explicit_environment_overrides_the_dotenv_file(self) -> None:
        # A developer following the README runs `DEVICE_BACKEND=mock ...` to
        # demo offline. If `.env` won, that command would quietly drive the
        # real device and spend model credits instead.
        with mock.patch.dict(
            os.environ,
            {"DEVICE_BACKEND": "mock", "DEVICE_PLANNER": "rules"},
            clear=False,
        ):
            with TestClient(create_app()) as client:
                health = client.get("/health").json()

        self.assertEqual(health["backend"], "mock")
        self.assertEqual(health["planner"], "rules")

    def test_console_and_static_assets_are_served(self) -> None:
        console = self.client.get("/")
        favicon = self.client.get("/favicon.ico")
        stylesheet = self.client.get("/static/styles.css")
        script = self.client.get("/static/app.js")

        self.assertEqual(console.status_code, 200)
        self.assertEqual(favicon.status_code, 204)
        self.assertIn("DeviceOps Console", console.text)
        self.assertIn("设备运维会话", console.text)
        self.assertIn("了解 DeviceOps", console.text)
        self.assertIn('id="starter-actions"', console.text)
        self.assertNotIn('id="context-diagnose"', console.text)
        self.assertIn('id="toggle-inspector"', console.text)
        self.assertIn('id="new-session"', console.text)
        self.assertIn('id="device-profile-select"', console.text)
        self.assertIn('id="device-power-budget"', console.text)
        self.assertIn('id="device-last-updated"', console.text)
        self.assertIn("v=1.6.0", console.text)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(".workspace", stylesheet.text)
        self.assertIn(".progress-card", stylesheet.text)
        self.assertIn(".message-suggestions", stylesheet.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn('runWorkflow("/v1/requests/stream"', script.text)
        self.assertIn("OUT OF SCOPE", script.text)
        self.assertIn("AbortController", script.text)
        self.assertIn("appendMessageSuggestions", script.text)
        self.assertIn("activateDevice", script.text)
        self.assertIn("/v1/devices/current/status", script.text)

    def test_query_endpoint_returns_completed_workflow(self) -> None:
        response = self.client.post(
            "/v1/requests",
            json={
                "request": "查询1号端口状态",
                "thread_id": "api-query",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"]["port"]["port_id"], 1)
        self.assertEqual(payload["suggestions"][0]["label"], "诊断 1 号端口")

    def test_device_inventory_is_sanitized(self) -> None:
        response = self.client.get("/v1/devices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["active_profile_id"], "default")
        self.assertFalse(payload["switching_enabled"])
        self.assertEqual(payload["devices"][0]["label"], "当前设备")
        self.assertNotIn("xdp_mcp_url", response.text)
        self.assertNotIn("device_id", response.text)

    def test_chat_and_out_of_scope_requests_are_normal_answers(self) -> None:
        chat = self.client.post(
            "/v1/requests",
            json={"request": "你是谁", "conversation_id": "api-chat"},
        )
        unsupported = self.client.post(
            "/v1/requests",
            json={
                "request": "今天天气怎么样？",
                "conversation_id": "api-scope",
            },
        )

        self.assertEqual(chat.status_code, 200)
        self.assertEqual(chat.json()["command"]["action"], "respond_chat")
        self.assertTrue(chat.json()["result"]["ok"])
        self.assertIn("DeviceOps", chat.json()["summary"])
        self.assertEqual(unsupported.status_code, 200)
        self.assertEqual(
            unsupported.json()["command"]["action"],
            "unsupported_request",
        )
        self.assertTrue(unsupported.json()["result"]["ok"])

    def test_control_endpoint_requires_explicit_resume(self) -> None:
        pending = self.client.post(
            "/v1/requests",
            json={
                "request": "打开2号端口",
                "thread_id": "api-control",
            },
        )

        self.assertEqual(
            pending.json()["status"],
            "confirmation_required",
        )

        completed = self.client.post(
            "/v1/requests/api-control/resume",
            json={"approved": True},
        )

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"], "completed")
        self.assertTrue(completed.json()["approved"])

    def test_stream_emits_each_diagnosis_step_before_the_run(self) -> None:
        events = self._stream(
            "/v1/requests/stream",
            {
                "request": "2号端口为什么无法充电",
                "thread_id": "api-stream-diagnose",
            },
        )

        names = [name for name, _ in events]
        self.assertEqual(names[-1], "run")
        self.assertEqual(names.count("run"), 1)
        self.assertNotIn("error", names)

        steps = [payload["step"] for name, payload in events if name == "trace"]
        self.assertEqual(
            steps,
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

        run = events[-1][1]["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["trace"], steps)

    def test_stream_pauses_on_confirmation_and_resumes(self) -> None:
        pending = self._stream(
            "/v1/requests/stream",
            {"request": "打开2号端口", "thread_id": "api-stream-control"},
        )
        pending_run = pending[-1][1]["run"]

        self.assertEqual(pending_run["status"], "confirmation_required")
        self.assertIn("prepare_control", pending_run["trace"])
        self.assertNotIn("control", pending_run["trace"])
        self.assertIsNotNone(pending_run["confirmation"])

        resumed = self._stream(
            "/v1/requests/api-stream-control/resume/stream",
            {"approved": True},
        )
        resumed_run = resumed[-1][1]["run"]

        self.assertEqual(resumed_run["status"], "completed")
        self.assertTrue(resumed_run["approved"])
        self.assertIn("control", resumed_run["trace"])
        self.assertIn("verify_control", resumed_run["trace"])

    def test_run_reports_timing_for_every_workflow_step(self) -> None:
        events = self._stream(
            "/v1/requests/stream",
            {"request": "2号端口为什么无法充电", "thread_id": "api-metrics"},
        )
        run = events[-1][1]["run"]
        metrics = run["metrics"]

        timed_nodes = [step["node"] for step in metrics["steps"]]
        self.assertEqual(timed_nodes, run["trace"])
        self.assertTrue(all(step["duration_ms"] >= 0 for step in metrics["steps"]))
        self.assertGreater(metrics["duration_ms"], 0)
        self.assertGreaterEqual(
            metrics["duration_ms"],
            metrics["workflow_ms"],
        )

        # Planning is still timed, but the rule-based planner calls no model,
        # so there are no tokens and nothing to bill.
        self.assertGreaterEqual(metrics["planner_ms"], 0.0)
        self.assertIsNone(metrics["usage"])
        self.assertIsNone(metrics["cost"])
        self.assertIsNone(metrics["model"])

        streamed = [
            payload["duration_ms"]
            for name, payload in events
            if name == "trace"
        ]
        self.assertEqual(len(streamed), len(metrics["steps"]))

    def test_resume_times_only_the_steps_it_executed(self) -> None:
        self._stream(
            "/v1/requests/stream",
            {"request": "打开2号端口", "thread_id": "api-metrics-resume"},
        )
        resumed = self._stream(
            "/v1/requests/api-metrics-resume/resume/stream",
            {"approved": True},
        )
        run = resumed[-1][1]["run"]
        timed_nodes = [step["node"] for step in run["metrics"]["steps"]]

        # `trace` accumulates across the whole thread; timings cover only the
        # segment this call ran, so they line up as its tail.
        self.assertLess(len(timed_nodes), len(run["trace"]))
        self.assertEqual(run["trace"][-len(timed_nodes):], timed_nodes)

    def test_stream_reports_contract_violation_as_an_error_event(self) -> None:
        # Headers are already sent once streaming starts, so a mid-run failure
        # has to surface as an event instead of an HTTP status code.
        events = self._stream(
            "/v1/requests/stream",
            {"request": "查询9号端口状态", "thread_id": "api-stream-invalid"},
        )

        names = [name for name, _ in events]
        self.assertEqual(names[-1], "error")
        self.assertNotIn("run", names)
        self.assertEqual(events[-1][1]["error_type"], "ValueError")
        self.assertIn("port 9 is not available", events[-1][1]["detail"])

    def _stream(
        self,
        url: str,
        body: dict[str, object],
    ) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        with self.client.stream("POST", url, json=body) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-type"].startswith(
                    "text/event-stream"
                )
            )
            name = ""
            for line in response.iter_lines():
                if line.startswith("event:"):
                    name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    events.append(
                        (name, json.loads(line[len("data:"):].strip()))
                    )
        return events

    def test_conversation_keeps_port_reference_between_requests(self) -> None:
        conversation_id = "api-conversation"
        first = self.client.post(
            "/v1/requests",
            json={
                "request": "现在几号端口在充电",
                "conversation_id": conversation_id,
            },
        )
        follow_up = self.client.post(
            "/v1/requests",
            json={
                "request": "把它关掉",
                "conversation_id": conversation_id,
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["summary"], "1号端口。")
        self.assertEqual(
            first.json()["conversation_id"],
            conversation_id,
        )
        self.assertEqual(
            follow_up.json()["status"],
            "confirmation_required",
        )
        self.assertEqual(follow_up.json()["command"]["port"], 1)
        self.assertFalse(follow_up.json()["command"]["enabled"])

    def test_automatic_control_policy_completes_in_one_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = asyncio.run(
                create_runtime(
                    RuntimeSettings.from_mapping(
                        {
                            "DEVICE_CONTROL_MODE": "automatic",
                            "DEVICE_AUDIT_PATH": str(
                                Path(temporary_directory) / "audit.jsonl"
                            ),
                            "DEVICE_SESSION_DB": str(
                                Path(temporary_directory) / "sessions.sqlite3"
                            ),
                        }
                    )
                )
            )
            with TestClient(create_app(runtime)) as client:
                response = client.post(
                    "/v1/requests",
                    json={"request": "打开2号端口"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertTrue(response.json()["approved"])
        self.assertEqual(
            response.json()["result"]["verification"]["status"],
            "verified",
        )
        self.assertEqual(
            [
                step["node"]
                for step in response.json()["metrics"]["steps"]
            ],
            response.json()["trace"],
        )


class DeviceSwitchApiTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        settings = RuntimeSettings.from_mapping(
            {
                "DEVICE_BACKEND": "mock",
                "DEVICE_PLANNER": "rules",
                "DEVICE_AUDIT_PATH": str(root / "audit.jsonl"),
                "DEVICE_SESSION_DB": str(root / "sessions.sqlite3"),
            }
        )
        catalog = DeviceProfileCatalog(
            active_profile="device-a",
            profiles=[
                DeviceProfile(
                    id="device-a",
                    label="设备 A",
                    backend="mock",
                    device_id="MOCK-A",
                    allowed_ports=[1, 2, 3],
                ),
                DeviceProfile(
                    id="device-b",
                    label="设备 B",
                    backend="mock",
                    device_id="MOCK-B",
                    allowed_ports=[1, 2, 3, 4, 5],
                ),
            ],
        )
        manager = asyncio.run(
            DeviceRuntimeCoordinator.create(settings, catalog)
        )
        self.client = TestClient(create_app(runtime_manager=manager))

    def test_switches_device_and_updates_health(self) -> None:
        response = self.client.post("/v1/devices/device-b/activate")
        inventory = self.client.get("/v1/devices").json()
        health = self.client.get("/health").json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["switched"])
        self.assertEqual(response.json()["snapshot"]["profile_id"], "device-b")
        self.assertNotIn("device_id", response.text)
        self.assertEqual(inventory["active_profile_id"], "device-b")
        self.assertEqual(health["allowed_ports"], [1, 2, 3, 4, 5])
        self.assertNotIn("device_id", health)

    def test_current_device_status_is_public_and_sanitized(self) -> None:
        response = self.client.get("/v1/devices/current/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["profile_id"], "device-a")
        self.assertEqual(response.json()["total_power_w"], 45.0)
        self.assertNotIn("MOCK-A", response.text)
        self.assertNotIn("device_id", response.text)

    def test_switching_devices_isolates_conversation_memory(self) -> None:
        conversation_id = "same-browser-conversation"
        first = self.client.post(
            "/v1/requests",
            json={
                "request": "查2号端口状态",
                "conversation_id": conversation_id,
            },
        )
        switched = self.client.post("/v1/devices/device-b/activate")
        follow_up = self.client.post(
            "/v1/requests",
            json={
                "request": "把它关掉",
                "conversation_id": conversation_id,
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(follow_up.status_code, 200)
        self.assertEqual(
            follow_up.json()["command"]["action"],
            "ask_clarification",
        )

    def test_unknown_device_profile_returns_404(self) -> None:
        response = self.client.post("/v1/devices/missing/activate")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("KeyError", response.text)


if __name__ == "__main__":
    unittest.main()

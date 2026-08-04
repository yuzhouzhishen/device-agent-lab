from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from device_agent_lab.api import create_app
from device_agent_lab.runtime import RuntimeSettings, create_runtime


class DeviceOpsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        audit_path = Path(temporary_directory.name) / "audit.jsonl"
        runtime = asyncio.run(
            create_runtime(
                RuntimeSettings.from_mapping(
                    {"DEVICE_AUDIT_PATH": str(audit_path)}
                )
            )
        )
        self.client = TestClient(create_app(runtime))

    def test_console_and_static_assets_are_served(self) -> None:
        console = self.client.get("/")
        stylesheet = self.client.get("/static/styles.css")
        script = self.client.get("/static/app.js")

        self.assertEqual(console.status_code, 200)
        self.assertIn("DeviceOps Console", console.text)
        self.assertIn("设备运维会话", console.text)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(".workspace", stylesheet.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn('postJson("/v1/requests"', script.text)

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


if __name__ == "__main__":
    unittest.main()

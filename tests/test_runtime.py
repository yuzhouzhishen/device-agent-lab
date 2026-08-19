from __future__ import annotations

import unittest

from device_agent_lab.device_gateway import MockDeviceGateway
from device_agent_lab.planner import RuleBasedCommandPlanner
from device_agent_lab.runtime import RuntimeSettings, create_runtime


class RuntimeSettingsTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_runtime_is_offline_and_self_contained(self) -> None:
        settings = RuntimeSettings.from_mapping({})

        runtime = await create_runtime(settings)

        self.assertEqual(settings.backend, "mock")
        self.assertIsInstance(runtime.gateway, MockDeviceGateway)
        self.assertIsInstance(runtime.planner, RuleBasedCommandPlanner)
        self.assertEqual(runtime.context.allowed_ports, [1, 2, 3])

    async def test_xdp_backend_requires_remote_url_from_environment(self) -> None:
        with self.assertRaisesRegex(ValueError, "XDP_MCP_URL"):
            RuntimeSettings.from_mapping({"DEVICE_BACKEND": "xdp"})

    async def test_ports_are_parsed_from_runtime_configuration(self) -> None:
        settings = RuntimeSettings.from_mapping(
            {"DEVICE_ALLOWED_PORTS": "1,2,4"}
        )

        self.assertEqual(settings.allowed_ports, (1, 2, 4))

    async def test_integrated_runtime_options_are_parsed(self) -> None:
        settings = RuntimeSettings.from_mapping(
            {
                "DEVICE_PLANNER": "ollama",
                "DEVICE_CONTROL_MODE": "automatic",
                "OLLAMA_MODEL": "llama3.1:8b",
                "FIRMWARE_RAG_URL": "http://127.0.0.1:8011",
                "FIRMWARE_RAG_TIMEOUT_SECONDS": "95",
                "GENERAL_KNOWLEDGE_FALLBACK": "true",
                "GENERAL_KNOWLEDGE_TIMEOUT_SECONDS": "25",
                "DEVICE_CONTROL_VERIFY_DELAY_SECONDS": "0.4",
                "DEVICE_CONTROL_VERIFY_ATTEMPTS": "5",
            }
        )

        self.assertEqual(settings.planner_mode, "ollama")
        self.assertEqual(settings.control_mode, "automatic")
        self.assertEqual(settings.knowledge_url, "http://127.0.0.1:8011")
        self.assertEqual(settings.knowledge_timeout_seconds, 95.0)
        self.assertTrue(settings.general_knowledge_fallback)
        self.assertEqual(settings.general_knowledge_timeout_seconds, 25.0)
        self.assertEqual(settings.control_verify_delay_seconds, 0.4)
        self.assertEqual(settings.control_verify_attempts, 5)

    async def test_relative_audit_path_is_resolved_from_project_root(self) -> None:
        settings = RuntimeSettings.from_mapping(
            {"DEVICE_AUDIT_PATH": "var/custom-audit.jsonl"}
        )

        self.assertTrue(settings.audit_path.is_absolute())
        self.assertEqual(settings.audit_path.name, "custom-audit.jsonl")
        self.assertEqual(settings.audit_path.parent.name, "var")


if __name__ == "__main__":
    unittest.main()

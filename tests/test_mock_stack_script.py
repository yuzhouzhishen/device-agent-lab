from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_mock_stack.py"
SPEC = importlib.util.spec_from_file_location("run_mock_stack", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
run_mock_stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_mock_stack)


class MockStackScriptTest(unittest.TestCase):
    def test_device_environment_forces_public_mock_boundaries(self) -> None:
        environment = run_mock_stack.build_device_environment(
            rag_url="http://127.0.0.1:8011",
            runtime_directory=Path("var/mock-demo"),
        )

        self.assertEqual(environment["DEVICE_BACKEND"], "mock")
        self.assertEqual(environment["DEVICE_PLANNER"], "rules")
        self.assertEqual(environment["DEVICE_CONTROL_MODE"], "automatic")
        self.assertNotIn("XDP_MCP_URL", environment)
        self.assertNotIn("DEVICE_PROFILES_FILE", environment)

    def test_rag_environment_is_offline_and_deterministic(self) -> None:
        environment = run_mock_stack.build_rag_environment(
            Path("/tmp/firmware-knowledge-agent")
        )

        self.assertEqual(environment["FIRMWARE_RAG_RETRIEVER"], "bm25")
        self.assertEqual(environment["FIRMWARE_RAG_GENERATOR"], "extractive")
        self.assertEqual(environment["FIRMWARE_RAG_RERANKER"], "none")

    def test_port_collision_is_reported_before_startup(self) -> None:
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = OSError(
            run_mock_stack.errno.EADDRINUSE,
            "Address already in use",
        )

        with mock.patch.object(
            run_mock_stack.socket,
            "socket",
            return_value=probe,
        ):
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                run_mock_stack.ensure_port_available("127.0.0.1", 8011)

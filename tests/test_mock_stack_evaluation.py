from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "evaluate_mock_stack.py"
)
SPEC = importlib.util.spec_from_file_location("evaluate_mock_stack", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MockStackEvaluationTest(unittest.TestCase):
    def test_request_contract_accepts_grounded_knowledge_response(self) -> None:
        case = {
            "expected_status": "completed",
            "expected_action": "answer_knowledge",
            "expected_response_kind": "knowledge",
            "expected_knowledge_status": "answered",
            "expected_citation_source": "esp-idf-nvs",
            "expected_trace_steps": ["answer_knowledge"],
        }
        payload = {
            "status": "completed",
            "command": {"action": "answer_knowledge"},
            "result": {"response_kind": "knowledge"},
            "knowledge_metadata": {"status": "answered"},
            "knowledge": [{"id": "esp-idf-nvs"}],
            "trace": ["validate", "answer_knowledge", "summarize"],
        }

        self.assertEqual(MODULE.evaluate_request_payload(case, payload), [])

    def test_request_contract_reports_control_verification_failure(self) -> None:
        case = {
            "expected_status": "completed",
            "expected_action": "set_port_power",
            "expected_verification_status": "verified",
            "expected_result_port_mode": "charging",
        }
        payload = {
            "status": "completed",
            "command": {"action": "set_port_power"},
            "result": {
                "verification": {"status": "error"},
                "after": {"data": {"mode": "standby"}},
            },
        }

        failures = MODULE.evaluate_request_payload(case, payload)

        self.assertEqual(len(failures), 2)
        self.assertIn("verification.status", failures[0])
        self.assertIn("result port mode", failures[1])

    def test_percentile_rejects_empty_measurements(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.percentile([], 0.95)


if __name__ == "__main__":
    unittest.main()

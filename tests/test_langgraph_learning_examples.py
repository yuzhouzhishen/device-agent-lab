from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def load_example(filename: str) -> dict[str, object]:
    path = EXAMPLES_DIR / filename
    if not path.exists():
        raise AssertionError(f"missing learning example: {filename}")
    module_name = f"langgraph_learning_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load learning example: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return vars(module)


class LangGraphLearningExamplesTest(unittest.TestCase):
    def test_06a_runs_one_node_from_start_to_end(self) -> None:
        example = load_example("06a_minimal_graph.py")

        result = example["run_demo"]()

        self.assertEqual(result, {"number": 2})

    def test_06b_routes_to_query_or_control(self) -> None:
        example = load_example("06b_conditional_routing.py")

        query_result = example["run_demo"]("query")
        control_result = example["run_demo"]("control")

        self.assertEqual(query_result["result"], "进入查询分支")
        self.assertEqual(control_result["result"], "进入控制分支")

    def test_06c_merges_partial_updates_and_accumulates_trace(self) -> None:
        example = load_example("06c_shared_state.py")

        result = example["run_demo"]()

        self.assertEqual(result["port_text"], " 2 ")
        self.assertEqual(result["port_id"], 2)
        self.assertEqual(result["result"], "port 2: standby")
        self.assertEqual(result["summary"], "查询完成：port 2: standby")
        self.assertEqual(result["trace"], ["normalize", "query", "summarize"])

    def test_06d_pauses_and_resumes_after_approval(self) -> None:
        example = load_example("06d_interrupt_resume.py")

        paused, completed = example["run_demo"](approved=True)

        self.assertIn("__interrupt__", paused)
        self.assertEqual(completed["status"], "executed")

    def test_06d_can_resume_with_rejection(self) -> None:
        example = load_example("06d_interrupt_resume.py")

        _, completed = example["run_demo"](approved=False)

        self.assertEqual(completed["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "benchmark_planner_latency.py"
)
SPEC = importlib.util.spec_from_file_location(
    "benchmark_planner_latency",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class PlannerLatencyBenchmarkTest(unittest.TestCase):
    def test_percentile_uses_nearest_rank(self) -> None:
        self.assertEqual(benchmark.percentile([1, 2, 3, 4], 0.5), 2)
        self.assertEqual(benchmark.percentile([1, 2, 3, 4], 0.95), 4)

    def test_percentile_rejects_empty_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            benchmark.percentile([], 0.95)

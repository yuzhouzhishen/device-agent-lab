from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from device_agent_lab.agent_contracts import DeviceAgentContext
from device_agent_lab.evaluation import (
    evaluate_planner,
    load_evaluation_cases,
    write_evaluation_report,
)
from device_agent_lab.planner import RuleBasedCommandPlanner


class PlannerEvaluationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dataset = Path("evals/planner_cases.json")
        self.holdout = Path("evals/planner_holdout_v1.json")
        self.context = DeviceAgentContext(
            device_id="EVAL-DEVICE",
            online=True,
            allowed_ports=[1, 2, 3, 4, 5],
            operator_role="evaluator",
        )

    async def test_rule_planner_matches_the_baseline_dataset(self) -> None:
        cases = load_evaluation_cases(self.dataset)

        report = await evaluate_planner(
            RuleBasedCommandPlanner(),
            self.context,
            cases,
            planner_name="rules",
        )

        self.assertEqual(len(cases), 40)
        self.assertEqual(report["passed_cases"], 40)
        self.assertEqual(report["exact_match_rate"], 1.0)
        self.assertEqual(
            report["categories"]["context"]["passed"],
            6,
        )
        self.assertEqual(
            report["categories"]["safety"]["passed"],
            4,
        )
        self.assertEqual(report["categories"]["chat"]["passed"], 3)
        self.assertEqual(report["categories"]["knowledge"]["passed"], 3)
        self.assertEqual(
            report["categories"]["unsupported"]["passed"],
            2,
        )
        self.assertEqual(report["failures"], [])

    async def test_report_can_be_written_as_json(self) -> None:
        cases = load_evaluation_cases(self.dataset)[:2]
        report = await evaluate_planner(
            RuleBasedCommandPlanner(),
            self.context,
            cases,
            planner_name="rules",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"

            write_evaluation_report(report, output)

            self.assertTrue(output.exists())
            self.assertIn(
                '"exact_match_rate": 1.0',
                output.read_text(encoding="utf-8"),
            )

    def test_holdout_is_frozen_and_disjoint_from_regression_set(self) -> None:
        regression = load_evaluation_cases(self.dataset)
        holdout = load_evaluation_cases(self.holdout)

        self.assertEqual(len(holdout), 18)
        self.assertEqual(
            {case.request for case in regression}
            & {case.request for case in holdout},
            set(),
        )
        self.assertEqual(
            len({case.id for case in holdout}),
            len(holdout),
        )


if __name__ == "__main__":
    unittest.main()

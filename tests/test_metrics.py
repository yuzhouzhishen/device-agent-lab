from __future__ import annotations

import unittest

from device_agent_lab.metrics import (
    RunMetrics,
    StepTiming,
    TokenPricing,
    TokenUsage,
    merge_run_metrics,
)
from device_agent_lab.runtime import RuntimeSettings


class _Message:
    def __init__(self, usage_metadata: dict[str, int] | None) -> None:
        if usage_metadata is not None:
            self.usage_metadata = usage_metadata


class TokenUsageTest(unittest.TestCase):
    def test_sums_usage_across_messages(self) -> None:
        usage = TokenUsage.from_messages(
            [
                _Message({"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}),
                _Message(None),
                _Message({"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}),
            ]
        )

        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 160)
        self.assertEqual(usage.output_tokens, 40)
        self.assertEqual(usage.total_tokens, 200)

    def test_returns_none_when_no_message_reports_usage(self) -> None:
        self.assertIsNone(TokenUsage.from_messages([_Message(None)]))
        self.assertIsNone(TokenUsage.from_messages([]))
        self.assertIsNone(TokenUsage.from_messages(None))

    def test_derives_total_when_the_provider_omits_it(self) -> None:
        usage = TokenUsage.from_messages(
            [_Message({"input_tokens": 90, "output_tokens": 10})]
        )

        assert usage is not None
        self.assertEqual(usage.total_tokens, 100)


class TokenPricingTest(unittest.TestCase):
    def test_prices_input_and_output_separately(self) -> None:
        pricing = TokenPricing(input_per_1m=2.0, output_per_1m=10.0)

        cost = pricing.cost_of(
            TokenUsage(input_tokens=500_000, output_tokens=100_000)
        )

        self.assertAlmostEqual(cost, 2.0)

    def test_settings_leave_pricing_unset_without_configuration(self) -> None:
        settings = RuntimeSettings.from_mapping({})

        self.assertIsNone(settings.pricing)

    def test_settings_read_prices_from_the_environment(self) -> None:
        settings = RuntimeSettings.from_mapping(
            {
                "DEVICE_TOKEN_PRICE_INPUT": "2.2",
                "DEVICE_TOKEN_PRICE_OUTPUT": "18",
                "DEVICE_TOKEN_PRICE_CURRENCY": "USD",
            }
        )

        self.assertIsNotNone(settings.pricing)
        assert settings.pricing is not None
        self.assertEqual(settings.pricing.input_per_1m, 2.2)
        self.assertEqual(settings.pricing.output_per_1m, 18.0)
        self.assertEqual(settings.pricing.currency, "USD")

    def test_settings_reject_unparsable_prices(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeSettings.from_mapping(
                {"DEVICE_TOKEN_PRICE_INPUT": "cheap"}
            )


class RunMetricsTest(unittest.TestCase):
    def test_merges_automatic_control_segments(self) -> None:
        merged = merge_run_metrics(
            RunMetrics(
                duration_ms=100,
                planner_ms=40,
                workflow_ms=60,
                steps=[StepTiming(node="validate", duration_ms=10)],
                usage=TokenUsage(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                ),
                cost=0.01,
                currency="CNY",
                model="local-model",
            ),
            RunMetrics(
                duration_ms=80,
                workflow_ms=80,
                steps=[StepTiming(node="control", duration_ms=70)],
            ),
        )

        self.assertEqual(merged.duration_ms, 180)
        self.assertEqual(
            [step.node for step in merged.steps],
            ["validate", "control"],
        )
        self.assertEqual(merged.usage.total_tokens, 120)
        self.assertEqual(merged.cost, 0.01)
        self.assertEqual(merged.model, "local-model")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from device_agent_lab.mock_device import DeviceController


class DeviceControllerTest(unittest.TestCase):
    def test_get_status_contains_three_ports(self) -> None:
        controller = DeviceController()

        status = controller.get_status()

        self.assertEqual(status["psn"], "DEMO-CP02-001")
        self.assertEqual(set(status["ports"].keys()), {"1", "2", "3"})

    def test_set_port_power_updates_total_power(self) -> None:
        controller = DeviceController()

        result = controller.set_port_power(2, enabled=True, power_w=30)

        self.assertTrue(result["ok"])
        self.assertEqual(result["device"]["ports"]["2"]["mode"], "charging")
        self.assertEqual(result["device"]["ports"]["2"]["power_w"], 30)
        self.assertEqual(result["device"]["total_power_w"], 75)

    def test_rejects_invalid_port(self) -> None:
        controller = DeviceController()

        with self.assertRaises(ValueError):
            controller.get_port_status(99)


if __name__ == "__main__":
    unittest.main()


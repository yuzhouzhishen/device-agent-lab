from __future__ import annotations

import json

from device_agent_lab.mock_device import DeviceController


def main() -> None:
    controller = DeviceController()

    print("# Initial device status")
    print(json.dumps(controller.get_status(), ensure_ascii=False, indent=2))

    print("\n# Turn on port 2 at 30W")
    print(json.dumps(controller.set_port_power(2, enabled=True, power_w=30), ensure_ascii=False, indent=2))

    print("\n# Query port 2")
    print(json.dumps(controller.get_port_status(2), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


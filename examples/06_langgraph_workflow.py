from __future__ import annotations

import json

from langgraph.types import Command

from device_agent_lab.agent_contracts import DeviceAgentContext, DeviceCommand
from device_agent_lab.device_workflow import build_device_workflow
from device_agent_lab.mock_device import DeviceController


def print_json(title: str, data: object) -> None:
    print(title)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print()


def main() -> None:
    context = DeviceAgentContext(
        device_id="DEMO-CP02-001",
        online=True,
        allowed_ports=[1, 2, 3],
        operator_role="实习调试员",
    )
    controller = DeviceController(psn=context.device_id)
    workflow = build_device_workflow(controller)
    command = DeviceCommand(
        intent="control_device",
        action="set_port_power",
        device_id=context.device_id,
        port=2,
        enabled=True,
        need_confirmation=True,
        reason="用户要求打开2号端口。",
    )
    config = {"configurable": {"thread_id": "langgraph-control-demo"}}

    print_json("initial port state:", controller.get_port_status(2))

    paused = workflow.invoke(
        {
            "command": command.model_dump(),
            "context": context.model_dump(),
            "trace": [],
        },
        config=config,
    )
    confirmation_request = paused["__interrupt__"][0].value
    print_json("workflow paused for confirmation:", confirmation_request)
    print_json("port state before confirmation:", controller.get_port_status(2))

    print("simulate user confirmation: yes\n")
    completed = workflow.invoke(Command(resume=False), config=config)

    print_json("workflow trace:", completed["trace"])
    print_json("execution result:", completed["result"])
    print("summary:")
    print(completed["summary"])


if __name__ == "__main__":
    main()

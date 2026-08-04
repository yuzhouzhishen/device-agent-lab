from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.types import Command

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    build_context_prompt,
)
from device_agent_lab.device_workflow import build_device_workflow
from device_agent_lab.mock_device import DeviceController


ApprovalHandler = Callable[[dict[str, Any]], bool]


def print_json(title: str, data: object) -> None:
    print(title)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print()


def build_demo_context() -> DeviceAgentContext:
    return DeviceAgentContext(
        device_id="DEMO-CP02-001",
        online=True,
        allowed_ports=[1, 2, 3],
        operator_role="实习调试员",
    )


def run_command_workflow(
    command: DeviceCommand,
    context: DeviceAgentContext,
    controller: DeviceController,
    approval_handler: ApprovalHandler,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    workflow = build_device_workflow(controller)
    config = {"configurable": {"thread_id": "agent-to-langgraph-demo"}}
    first_result = workflow.invoke(
        {
            "command": command.model_dump(),
            "context": context.model_dump(),
            "trace": [],
        },
        config=config,
    )

    interrupts = first_result.get("__interrupt__")
    if not interrupts:
        return None, first_result

    confirmation = interrupts[0].value
    approved = approval_handler(confirmation)
    completed = workflow.invoke(Command(resume=approved), config=config)
    return confirmation, completed


def ask_for_approval(confirmation: dict[str, Any]) -> bool:
    print_json("workflow requests confirmation:", confirmation)
    answer = input("确认执行这个设备操作吗？[y/N]: ").strip().lower()
    return answer in {"y", "yes", "是", "确认"}


def main() -> None:
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        print("GEMINI_API_KEY is not set, skip live model invocation.")
        return

    context = build_demo_context()
    controller = DeviceController(psn=context.device_id)
    model = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
        temperature=0,
    )
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt=build_context_prompt(context),
        response_format=DeviceCommand,
    )
    user_request = os.getenv("DEVICE_REQUEST", "帮我打开这台设备的2号端口。")
    print(f"user request: {user_request}\n")

    response = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": user_request,
                }
            ]
        }
    )
    command = response["structured_response"]
    print_json("agent structured command:", command.model_dump())

    _, completed = run_command_workflow(
        command,
        context,
        controller,
        ask_for_approval,
    )
    print_json("workflow trace:", completed["trace"])
    print_json("execution result:", completed["result"])
    print("summary:")
    print(completed["summary"])


if __name__ == "__main__":
    main()

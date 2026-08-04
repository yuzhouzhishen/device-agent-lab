from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI

from device_agent_lab.agent_contracts import (
    DeviceAgentContext,
    DeviceCommand,
    build_context_prompt,
)
from device_agent_lab.command_executor import execute_device_command
from device_agent_lab.mock_device import DeviceController


def build_demo_context() -> DeviceAgentContext:
    return DeviceAgentContext(
        device_id="DEMO-CP02-001",
        online=True,
        allowed_ports=[1, 2, 3],
        operator_role="实习调试员",
    )


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

    response = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "帮我打开这台设备的2号端口。",
                }
            ]
        }
    )
    command = response["structured_response"]

    print("agent command:")
    print(json.dumps(command.model_dump(), ensure_ascii=False, indent=2))
    print()

    first_result = execute_device_command(command, context, controller)
    print("first execution result:")
    print(json.dumps(first_result, ensure_ascii=False, indent=2))
    print()

    if first_result.get("requires_confirmation"):
        print("confirmation:")
        print("demo auto-confirms the command for learning purposes.")
        print()
        confirmed_result = execute_device_command(
            command,
            context,
            controller,
            confirmed=True,
        )
        print("confirmed execution result:")
        print(json.dumps(confirmed_result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

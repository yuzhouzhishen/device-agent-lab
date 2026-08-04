from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

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


def print_json(title: str, data: object) -> None:
    print(title)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print()


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

    checkpointer = InMemorySaver(
        serde=JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("device_agent_lab.agent_contracts", "DeviceCommand"),
            ]
        )
    )
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt=build_context_prompt(context),
        response_format=DeviceCommand,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "device-agent-short-term-demo"}}

    first_response = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "先帮我查看这台设备的2号端口状态。",
                }
            ]
        },
        config=config,
    )
    first_command = first_response["structured_response"]
    first_result = execute_device_command(first_command, context, controller)
    print_json("turn 1 command:", first_command.model_dump())
    print_json("turn 1 result:", first_result)

    second_response = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "把它打开。",
                }
            ]
        },
        config=config,
    )
    second_command = second_response["structured_response"]
    second_result = execute_device_command(second_command, context, controller)
    print_json("turn 2 command:", second_command.model_dump())
    print_json("turn 2 first result:", second_result)

    if second_result.get("requires_confirmation"):
        confirmed_result = execute_device_command(
            second_command,
            context,
            controller,
            confirmed=True,
        )
        print_json("turn 2 confirmed result:", confirmed_result)


if __name__ == "__main__":
    main()

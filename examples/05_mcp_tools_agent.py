from __future__ import annotations

import asyncio
import json
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient

from device_agent_lab.mcp_client_config import build_device_mcp_connection


def print_json(title: str, data: object) -> None:
    print(title)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print()


def find_tool(tools: list[BaseTool], name: str) -> BaseTool:
    for tool in tools:
        if tool.name == name:
            return tool
    available = ", ".join(tool.name for tool in tools)
    raise ValueError(f"tool {name!r} not found; available tools: {available}")


async def main() -> None:
    load_dotenv()

    client = MultiServerMCPClient(
        {
            "device": build_device_mcp_connection(),
        }
    )
    tools = await client.get_tools()
    print_json("mcp tools:", [tool.name for tool in tools])

    get_port_status = find_tool(tools, "device_get_port_status")
    direct_result = await get_port_status.ainvoke({"params": {"port_id": 2}})
    print_json("direct MCP tool result:", direct_result)

    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        print("GEMINI_API_KEY is not set, skip live Agent invocation.")
        return

    model = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
        temperature=0,
    )

    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=(
            "你是设备运维助手。"
            "你必须通过 MCP 工具读取或修改设备状态，不要编造设备数据。"
            "如果用户已经明确确认控制动作，可以调用控制工具。"
            "回答时用中文简洁说明你调用了什么工具以及设备返回了什么。"
        ),
    )

    response = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "我已确认，请通过 MCP 打开 2 号端口，然后告诉我结果。",
                }
            ]
        }
    )

    for message in response["messages"]:
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            print_json("agent tool calls:", tool_calls)
        if message.__class__.__name__ == "ToolMessage":
            print("agent tool result:")
            print(message.content)
            print()

    print("final answer:")
    print(response["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())

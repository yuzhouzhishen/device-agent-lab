from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI


@tool
def get_weather_for_location(city: str) -> str:
    """获取指定城市的天气信息。"""
    return f"在{city}总是阳光明媚！"


def main() -> None:
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        print("GEMINI_API_KEY is not set, skip live model invocation.")
        return

    model = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
        temperature=0,
    )

    agent = create_agent(
        model=model,
        tools=[get_weather_for_location],
        system_prompt="你是一位乐于助人的助手。",
    )

    response = agent.invoke(
        {"messages": [{"role": "user", "content": "北京的天气如何？"}]}
    )

    for message in response["messages"]:
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            print("tool calls:", tool_calls)
        if message.__class__.__name__ == "ToolMessage":
            print("tool result:", message.content)

    print("final answer:", response["messages"][-1].content)


if __name__ == "__main__":
    main()

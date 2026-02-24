"""Quick test: MiniMax tool-calling round-trip via LiteLLM."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import litellm

# litellm.set_verbose = True  # uncomment for full debug output

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    },
]


async def main():
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        print("Set LLM_API_KEY or MINIMAX_API_KEY env var")
        return

    model = "minimax/MiniMax-M2.5"
    api_base = "https://api.minimax.io/v1"

    messages = [
        {"role": "user", "content": "What's the weather in Tokyo?"},
    ]

    print(f"1) Sending request to {model}...")
    resp = await litellm.acompletion(
        model=model, messages=messages, tools=TOOLS,
        api_key=api_key, api_base=api_base,
    )

    msg = resp.choices[0].message
    print(f"   Content: {msg.content}")
    print(f"   Tool calls: {bool(msg.tool_calls)}")

    if not msg.tool_calls:
        print("   No tool call made — test done.")
        return

    tc = msg.tool_calls[0]
    print(f"   Tool: {tc.function.name}({tc.function.arguments})")

    # Build clean assistant message (same as our fix in agent.py)
    assistant_msg = {"role": "assistant", "content": msg.content or ""}
    assistant_msg["tool_calls"] = [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            },
        }
    ]
    messages.append(assistant_msg)

    # Fake tool result
    messages.append({
        "role": "tool",
        "tool_call_id": tc.id,
        "content": json.dumps({"temperature": "15°C", "condition": "Cloudy"}),
    })

    print("2) Sending tool result back...")
    resp2 = await litellm.acompletion(
        model=model, messages=messages, tools=TOOLS,
        api_key=api_key, api_base=api_base,
    )

    msg2 = resp2.choices[0].message
    print(f"   Final response: {msg2.content}")
    print("\nSUCCESS — full tool-calling round-trip works!")


asyncio.run(main())

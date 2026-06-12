"""
ReAct-style voice agent. Works with any instruction-tuned LLM via vLLM's OpenAI API.

Loop:
  1. Send messages to LLM
  2. If response contains TOOL: {...}, execute the tool, append result, loop
  3. If response contains ANSWER: ..., return that as the final reply
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

import httpx

from .config import settings
from .tools import TOOLS, TOOLS_SCHEMA

log = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are a helpful, friendly voice assistant. Your responses will be spoken aloud, so:
- Keep answers short (1-3 sentences max)
- Speak naturally and conversationally
- Never use markdown, bullet points, or lists
- Don't say "According to..." or "Based on..." — just answer directly
- IMPORTANT: Always reply in the same language the user spoke in. If they speak Hindi, reply in Hindi. If Gujarati, reply in Gujarati. If English, reply in English.

{TOOLS_SCHEMA}

To use a tool, respond with this exact format on its own line:
TOOL: {{"name": "tool_name", "args": {{"param": "value"}}}}

After receiving the tool result (shown as TOOL_RESULT: ...), continue thinking and use more tools if needed.

When you have the answer, respond with:
ANSWER: your spoken response here

Always end with ANSWER: even if you could not find information."""


@dataclass
class AgentEvent:
    type: str   # "tool_call" | "tool_result" | "answer" | "error"
    data: dict = field(default_factory=dict)


def _extract_json_object(text: str) -> str | None:
    """Extract the first balanced JSON object from text, handling nesting."""
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if c == '\\' and in_string:
            i += 2
            continue
        if c == '"':
            in_string = not in_string
        elif not in_string:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


async def run_agent(
    user_text: str,
    history: list[dict],
    on_event: Callable[[AgentEvent], None] | None = None,
) -> str:
    """
    Run the ReAct agent loop. Returns the final answer string.

    on_event is called for each intermediate step (tool calls, tool results)
    so the WebSocket gateway can stream progress to the client.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    for iteration in range(settings.agent_max_iterations):
        response_text = await _call_llm(messages)
        log.debug(f"LLM iteration {iteration + 1}: {response_text[:120]}")

        # Check for TOOL: call — use balanced-brace extraction to handle nested JSON
        tool_prefix = re.search(r"TOOL:\s*", response_text)
        tool_match = None
        if tool_prefix:
            json_str = _extract_json_object(response_text[tool_prefix.end():])
            tool_match = json_str  # string or None

        answer_match = re.search(r"ANSWER:\s*(.+)", response_text, re.DOTALL)

        if tool_match:
            try:
                tool_call = json.loads(tool_match)
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("args", {})
            except json.JSONDecodeError as e:
                log.warning(f"Bad tool JSON: {e}")
                messages.append({"role": "assistant", "content": response_text})
                messages.append({
                    "role": "user",
                    "content": f"TOOL_RESULT: JSON parse error — {e}. Please try again with valid JSON."
                })
                continue

            if on_event:
                coro = on_event(AgentEvent("tool_call", {"tool": tool_name, "args": tool_args}))
                if asyncio.iscoroutine(coro):
                    await coro

            result = await _execute_tool(tool_name, tool_args)
            log.info(f"Tool {tool_name}({tool_args}) → {result[:80]}")

            if on_event:
                coro = on_event(AgentEvent("tool_result", {"tool": tool_name, "result": result}))
                if asyncio.iscoroutine(coro):
                    await coro

            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": f"TOOL_RESULT: {result}"})
            continue

        if answer_match:
            answer = answer_match.group(1).strip()
            if on_event:
                coro = on_event(AgentEvent("answer", {"text": answer}))
                if asyncio.iscoroutine(coro):
                    await coro
            return answer

        # LLM responded without TOOL or ANSWER — treat whole response as answer
        log.warning(f"LLM response had no TOOL/ANSWER prefix: {response_text[:80]}")
        if on_event:
            coro = on_event(AgentEvent("answer", {"text": response_text}))
            if asyncio.iscoroutine(coro):
                await coro
        return response_text

    # Exhausted iterations
    fallback = "I'm sorry, I couldn't complete that request in time."
    if on_event:
        coro = on_event(AgentEvent("error", {"text": fallback}))
        if asyncio.iscoroutine(coro):
            await coro
    return fallback


async def _call_llm(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.post(
            f"{settings.llm_url}/v1/chat/completions",
            json={
                "model": settings.llm_model,
                "messages": messages,
                "max_tokens": 512,
                "temperature": 0.7,
                "stop": ["TOOL_RESULT:"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _execute_tool(name: str, args: dict) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return f"Unknown tool: {name}. Available: {list(TOOLS.keys())}"
    try:
        return await fn(**args)
    except TypeError as e:
        return f"Tool call error for {name}: {e}"
    except Exception as e:
        log.error(f"Tool {name} raised: {e}")
        return f"Tool {name} failed: {e}"

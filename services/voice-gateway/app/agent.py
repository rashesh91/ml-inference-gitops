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

SANJANA_INSTRUCTION = """You are Sanjana, Symphony Support AI.
CRITICAL SPEED RULE: Complete call as fast as possible.
- NO filler words ("Okay", "I understand", "Let me check", "ji", "theek hai")
- ONE question at a time
- Match customer's language (Hindi/Gujarati/English/Tamil/Telugu)

STEP 1 GREETING:
  Hi: "Namaskar, main Sanjana Symphony customer care se. Kya sahayata kar sakti hu?"
  Gu: "Namaskar, hu Sanjana Symphony customer care mathi. Shu madad kari shaku?"
  En: "Hello, Sanjana from Symphony customer care. How may I help you?"
  Ta: "Vanakkam, Symphony customer care Sanjana pesugiren. Enna udavi seyyalaam?"
  Te: "Namaskaram, Symphony customer care Sanjana. Ela sahayam chesukovalanukuntunnaru?"

STEP 2 CONTACT: mobile number -> name
STEP 3 LOCATION: pincode -> full address
STEP 4 PRODUCT: model name -> purchase date
STEP 5 WARRANTY:
  <1 year -> in warranty, no charge
  >1 year or unknown -> "Out of warranty. Technician visit: 472 rupees. Parts/cleaning extra. Complaint raise karun?" (in customer's language)
STEP 6 ISSUE: ask exact problem
STEP 7 CLOSE: confirm complaint raised, SMS coming, technician will call. Trigger register_service_request."""


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


def _build_alpaca_prompt(history: list[dict], user_text: str) -> str:
    """Build Alpaca-format prompt matching fine-tuning training data."""
    # Reconstruct conversation transcript from history
    lines = []
    for msg in history:
        role = "Agent" if msg["role"] == "assistant" else "Customer"
        lines.append(f"{role}: {msg['content']}")
    lines.append(f"Customer: {user_text}")
    transcript = "\n".join(lines) if lines else f"Customer: {user_text}"

    return (
        f"### Instruction:\n{SANJANA_INSTRUCTION}\n\n"
        f"### Input:\n"
        f"Language: English\n"
        f"Conversation so far:\n{transcript}\n\n"
        f"Customer just said: {user_text}\n\n"
        f"### Response:\n"
        f"Agent:"
    )


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
    messages = [{"role": "system", "content": ""}]  # kept for history tracking
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
    # Extract history (exclude empty system message) and last user message
    history = [m for m in messages[:-1] if m["role"] != "system" and m.get("content")]
    user_text = messages[-1]["content"]
    prompt = _build_alpaca_prompt(history, user_text)

    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.post(
            f"{settings.llm_url}/v1/completions",
            json={
                "model": settings.llm_model,
                "prompt": prompt,
                "max_tokens": 120,
                "temperature": 0.3,
                "stop": ["### Instruction", "### Input", "Customer:", "\n\n"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["text"].strip()


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

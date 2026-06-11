"""
Voice pipeline: audio bytes → transcript → LLM agent → TTS audio bytes
"""
import base64
import logging
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .agent import AgentEvent, run_agent
from .config import settings

log = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    history: list[dict] = field(default_factory=list)

    def add_turn(self, user: str, assistant: str):
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": assistant})
        max_msgs = settings.max_history_turns * 2
        if len(self.history) > max_msgs:
            self.history = self.history[-max_msgs:]


@dataclass
class PipelineResult:
    transcript: str
    answer: str
    audio_b64: str          # base64-encoded MP3
    tool_calls: list[dict]  # [{tool, args, result}, ...]


async def process_voice_turn(
    audio_bytes: bytes,
    session: Session,
    on_event: Callable[[str, dict], None] | None = None,
) -> PipelineResult:
    """
    Full voice turn pipeline.
    on_event(event_type, data) is called for progress updates sent over WebSocket.
    """
    # ── Step 1: Speech-to-Text ──────────────────────────────────────────────
    if on_event:
        on_event("status", {"message": "Transcribing your speech..."})

    transcript = await _transcribe(audio_bytes)
    log.info(f"[{session.session_id}] Transcript: '{transcript}'")

    if on_event:
        on_event("transcript", {"text": transcript})

    if not transcript.strip():
        return PipelineResult(
            transcript="",
            answer="I didn't catch that. Could you try again?",
            audio_b64=await _synthesize("I didn't catch that. Could you try again?"),
            tool_calls=[],
        )

    # ── Step 2: LLM Agent ───────────────────────────────────────────────────
    if on_event:
        on_event("status", {"message": "Thinking..."})

    tool_calls: list[dict] = []

    def handle_agent_event(evt: AgentEvent):
        if evt.type == "tool_call":
            if on_event:
                on_event("tool_call", evt.data)
        elif evt.type == "tool_result":
            tool_calls.append(evt.data)
            if on_event:
                on_event("tool_result", evt.data)

    answer = await run_agent(
        user_text=transcript,
        history=session.history,
        on_event=handle_agent_event,
    )
    log.info(f"[{session.session_id}] Answer: '{answer}'")

    # ── Step 3: Text-to-Speech ──────────────────────────────────────────────
    if on_event:
        on_event("status", {"message": "Generating speech..."})

    audio_b64 = await _synthesize(answer)
    if on_event:
        on_event("response", {"text": answer})

    # Update session history
    session.add_turn(transcript, answer)

    return PipelineResult(
        transcript=transcript,
        answer=answer,
        audio_b64=audio_b64,
        tool_calls=tool_calls,
    )


async def _transcribe(audio_bytes: bytes) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.stt_url}/transcribe",
            files={"audio": ("audio.webm", audio_bytes, "audio/webm")},
        )
        resp.raise_for_status()
        return resp.json()["text"]


async def _synthesize(text: str) -> str:
    """Returns base64-encoded MP3."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{settings.tts_url}/synthesize",
            json={"text": text, "voice": settings.tts_voice},
        )
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode()

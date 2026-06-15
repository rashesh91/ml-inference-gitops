"""
Voice pipeline: audio bytes → transcript → LLM agent → TTS audio bytes
"""
import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

from .agent import AgentEvent, run_agent
from .config import settings

log = logging.getLogger(__name__)

# OTel setup — no-ops gracefully if OTEL_EXPORTER_OTLP_ENDPOINT is unset
_otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
if _otlp_endpoint:
    _resource = Resource.create({"service.name": "voice-gateway", "service.version": "1.0.0"})
    _provider = TracerProvider(resource=_resource)
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp_endpoint)))
    trace.set_tracer_provider(_provider)

_tracer = trace.get_tracer("voice-gateway")


@dataclass
class Session:
    session_id: str
    history: list[dict] = field(default_factory=list)
    last_accessed: float = field(default_factory=time.time)

    def touch(self):
        self.last_accessed = time.time()

    def add_turn(self, user: str, assistant: str):
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": assistant})
        max_msgs = settings.max_history_turns * 2
        if len(self.history) > max_msgs:
            self.history = self.history[-max_msgs:]
        self.touch()


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
    async def emit(event_type: str, data: dict) -> None:
        if on_event:
            await on_event(event_type, data)

    with _tracer.start_as_current_span("voice-gateway.process_turn") as root_span:
        root_span.set_attribute("session.id", session.session_id)

    # ── Step 1: Speech-to-Text ──────────────────────────────────────────────
    await emit("status", {"message": "Transcribing your speech..."})

    with _tracer.start_as_current_span("stt.transcribe") as stt_span:
        stt_span.set_attribute("audio.size_bytes", len(audio_bytes))
        transcript = await _transcribe_with_retry(audio_bytes)
        stt_span.set_attribute("transcript.length", len(transcript))
    log.info(f"[{session.session_id}] Transcript: '{transcript}'")

    await emit("transcript", {"text": transcript})

    if not transcript.strip():
        return PipelineResult(
            transcript="",
            answer="I didn't catch that. Could you try again?",
            audio_b64=await _synthesize("I didn't catch that. Could you try again?"),
            tool_calls=[],
        )

    # ── Step 2: LLM Agent ───────────────────────────────────────────────────
    await emit("status", {"message": "Thinking..."})

    tool_calls: list[dict] = []

    async def handle_agent_event(evt: AgentEvent):
        if evt.type == "tool_call":
            await emit("tool_call", evt.data)
        elif evt.type == "tool_result":
            tool_calls.append(evt.data)
            await emit("tool_result", evt.data)

    with _tracer.start_as_current_span("agent.react_loop") as agent_span:
        agent_span.set_attribute("model", settings.llm_model)
        answer = await run_agent(
            user_text=transcript,
            history=session.history,
            on_event=handle_agent_event,
        )
        agent_span.set_attribute("tool_calls.count", len(tool_calls))
        agent_span.set_attribute("answer.length", len(answer))
    log.info(f"[{session.session_id}] Answer: '{answer}'")

    # ── Step 3: Text-to-Speech ──────────────────────────────────────────────
    await emit("status", {"message": "Generating speech..."})

    with _tracer.start_as_current_span("tts.synthesize") as tts_span:
        tts_span.set_attribute("text.length", len(answer))
        tts_span.set_attribute("voice", settings.tts_voice)
        audio_b64 = await _synthesize_with_retry(answer)
        tts_span.set_attribute("audio_b64.length", len(audio_b64))
    await emit("response", {"text": answer})

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


async def _transcribe_with_retry(audio_bytes: bytes, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            return await _transcribe(audio_bytes)
        except Exception as exc:
            if attempt == retries:
                raise
            wait = 0.4 * (attempt + 1)
            log.warning(f"STT attempt {attempt + 1} failed ({exc}), retrying in {wait}s")
            await asyncio.sleep(wait)
    raise RuntimeError("STT failed after retries")  # unreachable


async def _synthesize(text: str) -> str:
    """Returns base64-encoded MP3."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{settings.tts_url}/synthesize",
            json={"text": text, "voice": settings.tts_voice},
        )
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode()


async def _synthesize_with_retry(text: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            return await _synthesize(text)
        except Exception as exc:
            if attempt == retries:
                raise
            wait = 0.4 * (attempt + 1)
            log.warning(f"TTS attempt {attempt + 1} failed ({exc}), retrying in {wait}s")
            await asyncio.sleep(wait)
    raise RuntimeError("TTS failed after retries")  # unreachable

"""
Voice Gateway — FastAPI WebSocket server + REST API + static frontend.

WebSocket protocol (JSON messages):
  Client → Server:
    {type: "audio", data: "<base64 webm/ogg audio>"}   → triggers full pipeline
    {type: "text", text: "..."}                         → text-only (no TTS input)
    {type: "reset"}                                     → clear session history

  Server → Client:
    {type: "status", message: "..."}
    {type: "transcript", text: "..."}
    {type: "tool_call", tool: "...", args: {...}}
    {type: "tool_result", tool: "...", result: "..."}
    {type: "response", text: "..."}
    {type: "audio", data: "<base64 mp3>"}               → play this
    {type: "done"}
    {type: "error", message: "..."}
"""
import base64
import json
import logging
import re
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from .pipeline import Session, process_voice_turn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(title="Voice Agentic AI Gateway", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Prometheus metrics — KEDA ScaledObject watches websocket_active_connections
active_ws_connections = Gauge(
    "websocket_active_connections",
    "Number of active WebSocket sessions",
)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# In-memory sessions  (use Redis for multi-pod prod)
_sessions: dict[str, Session] = {}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "sessions_active": len(_sessions)}


# ── REST: text chat (useful for testing without microphone) ───────────────────

class TextChatRequest(BaseModel):
    session_id: str | None = None
    text: str


@app.post("/api/chat")
async def chat_text(req: TextChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    session = _sessions.setdefault(session_id, Session(session_id))

    from .agent import run_agent
    answer = await run_agent(req.text, session.history)
    session.add_turn(req.text, answer)

    from .pipeline import _synthesize
    audio_b64 = await _synthesize(answer)

    return {
        "session_id": session_id,
        "transcript": req.text,
        "answer": answer,
        "audio_b64": audio_b64,
    }


# ── REST: streaming text chat (SSE) ──────────────────────────────────────────

@app.post("/api/chat/stream")
async def chat_stream(req: TextChatRequest):
    """
    SSE endpoint for streaming LLM response sentence-by-sentence.
    Bridge calls this to overlap TTS generation with LLM generation.
    Events: data: {"sentence": "..."}\n\n  then  data: [DONE]\n\n
    """
    session_id = req.session_id or str(uuid.uuid4())
    session = _sessions.setdefault(session_id, Session(session_id))

    from .agent import run_agent_streaming

    full_sentences: list[str] = []

    async def generate():
        async for sentence in run_agent_streaming(req.text, session.history):
            full_sentences.append(sentence)
            yield f"data: {json.dumps({'sentence': sentence})}\n\n"
        session.add_turn(req.text, " ".join(full_sentences))
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Latency API ───────────────────────────────────────────────────────────────

def _parse_kv(line: str) -> dict:
    """Parse 'key=val key2=val2' pairs from a log line."""
    return dict(m.groups() for m in re.finditer(r'(\w+)=([^\s]+)', line))


@app.get("/api/latency")
async def latency_stats(hours: int = Query(default=24, ge=1, le=168)):
    """
    Parse LATENCY and CALL_SUMMARY lines from voice-bridge journal.
    Returns per-turn breakdown and per-call summaries.
    """
    try:
        result = subprocess.run(
            ["journalctl", "-u", "voice-bridge",
             f"--since={hours} hours ago", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.splitlines()
    except Exception as e:
        return JSONResponse({"error": str(e), "turns": [], "calls": [], "summary": {}})

    turns, calls = [], []
    for line in lines:
        if "LATENCY " in line:
            kv = _parse_kv(line)
            try:
                turns.append({
                    "session":     kv.get("session", ""),
                    "turn":        int(kv.get("turn", 0)),
                    "lang":        kv.get("lang", ""),
                    "silence_ms":  int(kv.get("silence_ms", 0)),
                    "stt_ms":      int(kv.get("stt_ms", 0)),
                    "llm_ms":      int(kv.get("llm_ms", 0)),
                    "tts_ms":      int(kv.get("tts_ms", 0)),
                    "ffmpeg_ms":   int(kv.get("ffmpeg_ms", 0)),
                    "total_ms":    int(kv.get("total_ms", 0)),
                    "cache_hit":   kv.get("cache_hit", "0") == "1",
                })
            except (ValueError, KeyError):
                pass
        elif "CALL_SUMMARY " in line:
            kv = _parse_kv(line)
            try:
                calls.append({
                    "session":  kv.get("session", ""),
                    "turns":    int(kv.get("turns", 0)),
                    "avg_ms":   int(kv.get("avg_ms", 0)),
                    "min_ms":   int(kv.get("min_ms", 0)),
                    "max_ms":   int(kv.get("max_ms", 0)),
                    "lang":     kv.get("lang", ""),
                    "hangup":   kv.get("hangup", ""),
                    "cache_hits": kv.get("cache_hits", "n/a"),
                })
            except (ValueError, KeyError):
                pass

    # Aggregate summary
    total_ms_list = [t["total_ms"] for t in turns if t["total_ms"] > 0]
    summary = {}
    if total_ms_list:
        summary = {
            "total_turns":   len(turns),
            "total_calls":   len(calls),
            "avg_ms":        int(sum(total_ms_list) / len(total_ms_list)),
            "min_ms":        min(total_ms_list),
            "max_ms":        max(total_ms_list),
            "p50_ms":        sorted(total_ms_list)[len(total_ms_list) // 2],
            "p95_ms":        sorted(total_ms_list)[int(len(total_ms_list) * 0.95)],
            "under_500ms":   sum(1 for ms in total_ms_list if ms < 500),
            "under_1500ms":  sum(1 for ms in total_ms_list if ms < 1500),
            "under_2500ms":  sum(1 for ms in total_ms_list if ms < 2500),
            "avg_silence_ms": int(sum(t["silence_ms"] for t in turns) / len(turns)),
            "avg_stt_ms":    int(sum(t["stt_ms"] for t in turns) / len(turns)),
            "avg_llm_ms":    int(sum(t["llm_ms"] for t in turns) / len(turns)),
            "avg_tts_ms":    int(sum(t["tts_ms"] for t in turns) / len(turns)),
            "cache_hits":    sum(1 for t in turns if t["cache_hit"]),
        }
    else:
        summary = {
            "total_turns": 0, "total_calls": 0,
            "avg_ms": 0, "min_ms": 0, "max_ms": 0,
            "p50_ms": 0, "p95_ms": 0,
            "under_500ms": 0, "under_1500ms": 0, "under_2500ms": 0,
            "avg_silence_ms": 0, "avg_stt_ms": 0, "avg_llm_ms": 0, "avg_tts_ms": 0,
            "cache_hits": 0,
        }

    return {"summary": summary, "turns": turns[-200:], "calls": calls[-50:]}


@app.get("/latency")
async def latency_dashboard():
    dashboard = STATIC_DIR / "latency.html"
    return HTMLResponse(dashboard.read_text())


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    index = STATIC_DIR / "index.html"
    return HTMLResponse(index.read_text())


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    await ws.accept()
    active_ws_connections.inc()
    session = _sessions.setdefault(session_id, Session(session_id))
    log.info(f"WebSocket connected: {session_id}")

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")

            if msg_type == "reset":
                session.history.clear()
                await ws.send_json({"type": "status", "message": "Session reset."})
                continue

            if msg_type == "text":
                # Text-only turn (no STT needed)
                await _handle_text_turn(ws, session, msg.get("text", ""))
                continue

            if msg_type == "audio":
                audio_b64 = msg.get("data", "")
                if not audio_b64:
                    await ws.send_json({"type": "error", "message": "No audio data"})
                    continue
                audio_bytes = base64.b64decode(audio_b64)
                await _handle_voice_turn(ws, session, audio_bytes)
                continue

            await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        log.info(f"WebSocket disconnected: {session_id}")
    except Exception as e:
        log.error(f"WebSocket error [{session_id}]: {e}", exc_info=True)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        active_ws_connections.dec()


async def _handle_voice_turn(ws: WebSocket, session: Session, audio_bytes: bytes):
    async def on_event(event_type: str, data: dict):
        await ws.send_json({"type": event_type, **data})

    try:
        result = await process_voice_turn(audio_bytes, session, on_event=on_event)
        await ws.send_json({"type": "audio", "data": result.audio_b64})
        await ws.send_json({"type": "done"})
    except Exception as e:
        log.error(f"Pipeline error: {e}", exc_info=True)
        await ws.send_json({"type": "error", "message": f"Pipeline failed: {e}"})


async def _handle_text_turn(ws: WebSocket, session: Session, text: str):
    from .agent import AgentEvent, run_agent
    from .pipeline import _synthesize

    tool_calls = []

    def handle_event(evt: AgentEvent):
        pass  # collected separately below

    await ws.send_json({"type": "status", "message": "Thinking..."})
    answer = await run_agent(text, session.history)
    session.add_turn(text, answer)

    await ws.send_json({"type": "response", "text": answer})
    await ws.send_json({"type": "status", "message": "Generating speech..."})
    audio_b64 = await _synthesize(answer)
    await ws.send_json({"type": "audio", "data": audio_b64})
    await ws.send_json({"type": "done"})

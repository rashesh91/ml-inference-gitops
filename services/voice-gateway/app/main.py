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
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .pipeline import Session, process_voice_turn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(title="Voice Agentic AI Gateway", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    index = STATIC_DIR / "index.html"
    return HTMLResponse(index.read_text())


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    await ws.accept()
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

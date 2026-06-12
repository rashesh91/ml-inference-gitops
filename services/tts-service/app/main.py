import asyncio
import io
import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from gtts import gTTS
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DEFAULT_VOICE = os.getenv("TTS_VOICE", "en-US-JennyNeural")
# gTTS lang mapping — fall back to "en" for any en-* voice
_LANG = "en"

app = FastAPI(title="TTS Service", version="1.0.0")


class SynthesizeRequest(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    rate: str = "+0%"
    pitch: str = "+0Hz"


class VoiceInfo(BaseModel):
    name: str
    locale: str
    gender: str


@app.get("/health")
async def health():
    return {"status": "ok", "default_voice": DEFAULT_VOICE, "backend": "gtts"}


@app.get("/voices", response_model=list[VoiceInfo])
async def list_voices():
    return [VoiceInfo(name="en-US-Standard", locale="en-US", gender="Female")]


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest):
    """Synthesize text to speech. Returns MP3 audio bytes."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")

    t0 = time.time()
    buf = io.BytesIO()
    tts = gTTS(text=req.text, lang=_LANG, slow=False)
    tts.write_to_fp(buf)
    audio_bytes = buf.getvalue()
    latency_ms = (time.time() - t0) * 1000
    log.info(f"Synthesized {len(req.text)} chars in {latency_ms:.0f}ms → {len(audio_bytes)} bytes")

    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={
            "X-Latency-Ms": str(int(latency_ms)),
            "X-Voice": req.voice,
        },
    )

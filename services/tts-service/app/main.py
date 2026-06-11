import asyncio
import logging
import os
import time

import edge_tts
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DEFAULT_VOICE = os.getenv("TTS_VOICE", "en-US-JennyNeural")

app = FastAPI(title="TTS Service", version="1.0.0")


class SynthesizeRequest(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    rate: str = "+0%"   # e.g. "+10%", "-20%"
    pitch: str = "+0Hz"


class VoiceInfo(BaseModel):
    name: str
    locale: str
    gender: str


@app.get("/health")
async def health():
    return {"status": "ok", "default_voice": DEFAULT_VOICE}


@app.get("/voices", response_model=list[VoiceInfo])
async def list_voices():
    voices = await edge_tts.list_voices()
    return [
        VoiceInfo(name=v["Name"], locale=v["Locale"], gender=v["Gender"])
        for v in voices
        if v["Locale"].startswith("en-")
    ]


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest):
    """Synthesize text to speech. Returns MP3 audio bytes."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")

    t0 = time.time()
    audio_chunks: list[bytes] = []

    communicate = edge_tts.Communicate(
        req.text,
        req.voice,
        rate=req.rate,
        pitch=req.pitch,
    )
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])

    audio_bytes = b"".join(audio_chunks)
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

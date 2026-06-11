import io
import logging
import os
import tempfile
import time

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
DEVICE = os.getenv("WHISPER_DEVICE", "auto")   # auto detects cuda if available
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "float16")  # int8 for CPU

app = FastAPI(title="Whisper STT Service", version="1.0.0")

log.info(f"Loading Whisper model: {MODEL_SIZE} on {DEVICE}")
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
log.info("Whisper model loaded")


class TranscribeResponse(BaseModel):
    text: str
    language: str
    duration: float
    latency_ms: float


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE}


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(audio: UploadFile = File(...)):
    """Transcribe an audio file. Accepts WAV, MP3, WebM, OGG, FLAC."""
    t0 = time.time()
    audio_bytes = await audio.read()

    if len(audio_bytes) < 100:
        raise HTTPException(status_code=400, detail="Audio too short")

    with tempfile.NamedTemporaryFile(suffix=".audio", delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        segments, info = model.transcribe(
            tmp.name,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(s.text.strip() for s in segments)

    latency_ms = (time.time() - t0) * 1000
    log.info(f"Transcribed {info.duration:.1f}s audio in {latency_ms:.0f}ms: '{text[:60]}'")

    return TranscribeResponse(
        text=text.strip(),
        language=info.language,
        duration=info.duration,
        latency_ms=latency_ms,
    )

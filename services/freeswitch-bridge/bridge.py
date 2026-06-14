#!/usr/bin/env python3
"""
Sanjana FreeSWITCH AI Bridge — v5 (sub-500ms)
-----------------------------------------------
New in v5 (3-phase latency optimisation):

  Phase 1 — FIFO + Sarvam streaming STT  (saves ~800ms)
    Record to named FIFO pipe; stream real-time PCM to Sarvam's
    wss streaming endpoint; Sarvam's built-in VAD fires end-of-speech
    instantly — no silence polling wait.

  Phase 2 — vLLM streaming + sentence-level TTS  (saves ~400ms)
    Call /api/chat/stream SSE endpoint; TTS each sentence in a thread
    as soon as it arrives; play sentence 1 while TTS generates sentence 2.

  Phase 3 — IVR response cache  (saves ~1200ms on ~80% of turns)
    Pre-TTS all 10 Symphony IVR phrases × 3 languages at startup.
    After each LLM response, fuzzy-match to cache; on hit skip TTS.
    Step-tracker predicts next phrase → on next turn skip LLM too.

Target latency:
  Cache hit  : ~300ms   (silence 50 + stt 200 + llm 0 + tts 0 + misc 50)
  Cache miss : ~700ms   (silence 50 + stt 200 + llm-stream 200 + tts 200 + misc 50)
  80% hit avg: ~380ms  ✅  (feels like natural conversation)

Latency log format:
  LATENCY session=X turn=N lang=hi silence_ms=50 stt_ms=200 llm_ms=0
          tts_ms=0 total_ms=300 cache=hit
  CALL_SUMMARY session=X turns=6 avg_ms=380 min_ms=295 max_ms=720
               lang=hi hangup=NORMAL_CLEARING cache_hits=5/6
"""
import asyncio
import base64
import concurrent.futures
import contextlib
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
import uuid

import requests
import websockets

import ivr_cache

# ── Config ────────────────────────────────────────────────────────────────────
BRIDGE_HOST        = "0.0.0.0"
BRIDGE_PORT        = int(os.getenv("BRIDGE_PORT",        "8086"))
GATEWAY_BASE       = os.getenv("GATEWAY_BASE",           "https://aitest.lintel.in")
RECORD_SILENCE_SEC = float(os.getenv("RECORD_SILENCE_SEC", "1"))   # 1s saves 1s latency
RECORD_MAX_SEC     = int(os.getenv("RECORD_MAX_SEC",     "15"))
TMP_DIR            = os.getenv("TMP_DIR",                "/tmp/sanjana")
GATEWAY_RETRY_MAX  = 1
FALLBACK_WAV       = os.getenv(
    "FALLBACK_WAV",
    "/usr/share/freeswitch/sounds/en/us/callie/ivr/8000/ivr-please_try_again.wav",
)

# Provider selection
STT_PROVIDER   = os.getenv("STT_PROVIDER",   "sarvam")   # sarvam | whisper
TTS_PROVIDER   = os.getenv("TTS_PROVIDER",   "sarvam")   # sarvam | gtts
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")

SARVAM_STT_URL    = "https://api.sarvam.ai/speech-to-text"
SARVAM_STT_WS_URL = "wss://api.sarvam.ai/speech-to-text-streaming"
SARVAM_TTS_URL    = "https://api.sarvam.ai/text-to-speech"

# ── Language menu ─────────────────────────────────────────────────────────────
MENU_TEXT = (
    "Welcome to Symphony customer care. "
    "Hindi ke liye 1 dabaye. "
    "For English press 2. "
    "Gujarati mate 3 dabavo."
)

LANGUAGES = {
    "1": {
        "name":        "Hindi",
        "lang_code":   "hi-IN",
        "sarvam_spk":  "meera",
        "greeting":    "Namaskar, main Sanjana Symphony customer care se. "
                       "Kya sahayata kar sakti hu?",
    },
    "2": {
        "name":        "English",
        "lang_code":   "en-IN",
        "sarvam_spk":  "meera",
        "greeting":    "Hello, I'm Sanjana from Symphony customer care. "
                       "How may I help you?",
    },
    "3": {
        "name":        "Gujarati",
        "lang_code":   "gu-IN",
        "sarvam_spk":  "meera",
        "greeting":    "Namaskar, hu Sanjana Symphony customer care mathi. "
                       "Shu madad kari shaku?",
    },
}
DEFAULT_LANG_KEY = "2"

_audio_cache: dict[str, str] = {}   # label → WAV path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("bridge")
os.makedirs(TMP_DIR, exist_ok=True)


# ── Latency instrumentation ───────────────────────────────────────────────────

class TurnTimer:
    """Tracks per-stage latency for one conversation turn."""

    def __init__(self):
        self._start = time.monotonic()
        self._marks: dict[str, float] = {}

    def mark(self, stage: str):
        self._marks[stage] = time.monotonic() - self._start

    def total_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def log(self, session_id: str, turn: int, lang: str):
        self.log_extra(session_id, turn, lang)

    def log_extra(self, session_id: str, turn: int, lang: str, **kv):
        parts = " ".join(f"{k}_ms={int(v * 1000)}" for k, v in self._marks.items())
        extra = " ".join(f"{k}={v}" for k, v in kv.items())
        log.info(
            f"LATENCY session={session_id} turn={turn} lang={lang} "
            f"{parts} total_ms={self.total_ms()}"
            + (f" {extra}" if extra else "")
        )


class CallStats:
    """Accumulates per-turn totals for end-of-call summary."""

    def __init__(self):
        self._totals: list[int] = []

    def add(self, ms: int):
        self._totals.append(ms)

    def log_summary(
        self, session_id: str, lang: str,
        hangup: str = "", cache_hits: int = 0, total_turns: int = 0,
    ):
        if not self._totals:
            return
        avg = int(sum(self._totals) / len(self._totals))
        hit_rate = f"{cache_hits}/{total_turns}" if total_turns else "n/a"
        log.info(
            f"CALL_SUMMARY session={session_id} turns={len(self._totals)} "
            f"avg_ms={avg} min_ms={min(self._totals)} max_ms={max(self._totals)} "
            f"lang={lang} hangup={hangup} cache_hits={hit_rate}"
        )


# ── STT ───────────────────────────────────────────────────────────────────────

def _stt_sarvam(wav_path: str, lang_code: str) -> str | None:
    """Sarvam saarika:v2 — best accuracy for Indian languages, ~250ms."""
    if not SARVAM_API_KEY:
        log.error("SARVAM_API_KEY not set — cannot use Sarvam STT")
        return None
    try:
        with open(wav_path, "rb") as f:
            resp = requests.post(
                SARVAM_STT_URL,
                headers={"api-subscription-key": SARVAM_API_KEY},
                files={"file": ("audio.wav", f, "audio/wav")},
                data={"model": "saarika:v2", "language_code": lang_code},
                timeout=20,
            )
        resp.raise_for_status()
        return resp.json().get("transcript", "").strip() or None
    except Exception as e:
        log.error(f"Sarvam STT error: {e}")
        return None


def _stt_whisper(wav_path: str) -> str | None:
    """Internal Whisper STT via /stt/transcribe ingress path."""
    try:
        with open(wav_path, "rb") as f:
            resp = requests.post(
                f"{GATEWAY_BASE}/stt/transcribe",
                files={"audio": ("audio.wav", f, "audio/wav")},
                timeout=20,
            )
        resp.raise_for_status()
        return resp.json().get("text", "").strip() or None
    except Exception as e:
        log.error(f"Whisper STT error: {e}")
        return None


def _transcribe(wav_path: str, lang_code: str) -> str | None:
    if STT_PROVIDER == "sarvam":
        return _stt_sarvam(wav_path, lang_code)
    return _stt_whisper(wav_path)


async def _sarvam_streaming_stt(
    fifo_path: str,
    lang_code: str,
    call_uuid: str,
    sock: socket.socket,
) -> str | None:
    """
    Phase 1: Stream real-time PCM from FIFO to Sarvam's streaming STT WebSocket.
    Sarvam's built-in VAD fires is_final the moment the caller stops speaking —
    no silence polling, saving ~800ms vs file-based recording with 1s wait.

    Called via asyncio.run() from the synchronous handle_call thread.
    FS must have already executed the `record` command (write end of FIFO opened).
    """
    if not SARVAM_API_KEY:
        return None

    url = (
        f"{SARVAM_STT_WS_URL}"
        f"?api-subscription-key={SARVAM_API_KEY}"
        f"&model=saarika:v2"
        f"&language_code={lang_code}"
    )

    transcript: str | None = None
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    _fifo_file = None

    async def _recv(ws):
        nonlocal transcript
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("is_final"):
                        transcript = data.get("transcript", "").strip() or None
                        done.set()
                        return
        except Exception as e:
            log.debug(f"WS recv closed: {e}")
        finally:
            done.set()

    async def _send(ws):
        nonlocal _fifo_file
        CHUNK = 3200  # 100ms @ 16kHz PCM16 (100ms × 16000 samples/s × 2 bytes)
        try:
            # os.open O_RDONLY blocks until FS opens write end — run in executor
            fd = await loop.run_in_executor(None, os.open, fifo_path, os.O_RDONLY)
            _fifo_file = os.fdopen(fd, "rb")
            while not done.is_set():
                chunk = await loop.run_in_executor(None, _fifo_file.read, CHUNK)
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                await ws.send(chunk)
        except Exception as e:
            log.debug(f"FIFO send stopped: {e}")
        finally:
            if _fifo_file:
                with contextlib.suppress(Exception):
                    _fifo_file.close()
            done.set()

    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            recv_task = asyncio.create_task(_recv(ws))
            send_task = asyncio.create_task(_send(ws))
            try:
                await asyncio.wait_for(done.wait(), timeout=RECORD_MAX_SEC + 5)
            except asyncio.TimeoutError:
                log.warning(f"Streaming STT timed out after {RECORD_MAX_SEC}s")
            finally:
                recv_task.cancel()
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(recv_task, send_task)
    except Exception as e:
        log.error(f"Sarvam streaming STT connect failed: {e}")
    finally:
        # Signal FS to stop recording (no-op if already stopped by hangup)
        with contextlib.suppress(Exception):
            _api(sock, f"uuid_break {call_uuid} all")

    return transcript


# ── TTS ───────────────────────────────────────────────────────────────────────

def _tts_sarvam(text: str, lang_code: str, speaker: str) -> bytes | None:
    """
    Sarvam bulbul:v1 — returns base64 WAV at 8000Hz directly.
    No ffmpeg conversion needed — saves ~120ms per turn.
    """
    if not SARVAM_API_KEY:
        log.error("SARVAM_API_KEY not set — cannot use Sarvam TTS")
        return None
    try:
        resp = requests.post(
            SARVAM_TTS_URL,
            headers={
                "api-subscription-key": SARVAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": lang_code,
                "speaker": speaker,
                "speech_sample_rate": 8000,   # telephony rate
                "enable_preprocessing": True,
                "model": "bulbul:v1",
            },
            timeout=15,
        )
        resp.raise_for_status()
        b64 = resp.json().get("audios", [""])[0]
        return base64.b64decode(b64) if b64 else None
    except Exception as e:
        log.error(f"Sarvam TTS error: {e}")
        return None


def _tts_gtts(text: str) -> bytes | None:
    """Internal gTTS via /tts/synthesize (returns MP3 — needs ffmpeg)."""
    try:
        resp = requests.post(
            f"{GATEWAY_BASE}/tts/synthesize",
            json={"text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.content or None
    except Exception as e:
        log.error(f"gTTS error: {e}")
        return None


def _synthesize(text: str, lang_code: str, speaker: str) -> tuple[bytes | None, bool]:
    """
    Returns (audio_bytes, is_wav).
    is_wav=True  → bytes are already WAV (Sarvam), play directly
    is_wav=False → bytes are MP3 (gTTS), need ffmpeg conversion
    """
    if TTS_PROVIDER == "sarvam":
        return _tts_sarvam(text, lang_code, speaker), True
    return _tts_gtts(text), False


# ── LLM ───────────────────────────────────────────────────────────────────────

def _chat(text: str, session_id: str, lang_code: str, speaker: str) -> bytes | None:
    """
    POST to /api/chat → LLM answer + TTS audio.
    If gateway TTS is disabled (Sarvam TTS preferred), we use audio_b64 as fallback.
    """
    for attempt in range(1, GATEWAY_RETRY_MAX + 2):
        try:
            resp = requests.post(
                f"{GATEWAY_BASE}/api/chat",
                json={"text": text, "session_id": session_id},
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()
            answer_text = data.get("answer", "")

            # Prefer Sarvam TTS over gateway's gTTS for better Indian language quality
            if TTS_PROVIDER == "sarvam" and answer_text:
                wav_bytes = _tts_sarvam(answer_text, lang_code, speaker)
                if wav_bytes:
                    return wav_bytes   # already WAV

            # Fall back to gateway's audio_b64 (MP3 from gTTS)
            b64 = data.get("audio_b64", "")
            return base64.b64decode(b64) if b64 else None

        except Exception as e:
            log.warning(f"Chat attempt {attempt}: {e}")
            if attempt <= GATEWAY_RETRY_MAX:
                time.sleep(0.5)
    return None


def _chat_streaming(
    text: str,
    session_id: str,
    lang_code: str,
    speaker: str,
) -> list[tuple[str, "concurrent.futures.Future[bytes | None]"]] | None:
    """
    Phase 2: Stream LLM sentences from /api/chat/stream SSE endpoint.
    Submits each sentence to Sarvam TTS immediately in a thread pool.
    Caller plays sentence 1 while sentence 2's TTS is still running.

    Returns list of (sentence_text, Future[wav_bytes]) or None if endpoint
    unavailable (caller falls back to _chat()).
    """
    try:
        resp = requests.post(
            f"{GATEWAY_BASE}/api/chat/stream",
            json={"text": text, "session_id": session_id},
            stream=True,
            timeout=35,
        )
        resp.raise_for_status()
    except requests.ConnectionError:
        return None   # endpoint not available — fallback to _chat()
    except Exception as e:
        log.error(f"Streaming chat request failed: {e}")
        return None

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="tts")
    futures: list[tuple[str, concurrent.futures.Future]] = []

    try:
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                sentence = json.loads(payload).get("sentence", "")
            except json.JSONDecodeError:
                continue
            if sentence:
                fut = executor.submit(_tts_sarvam, sentence, lang_code, speaker)
                futures.append((sentence, fut))
    except Exception as e:
        log.error(f"Streaming chat SSE read error: {e}")
    finally:
        executor.shutdown(wait=False)

    return futures if futures else None


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _mp3_to_wav(mp3_path: str) -> str | None:
    """Convert MP3 → 8kHz mono PCM WAV using ffmpeg."""
    wav_path = mp3_path.replace(".mp3", ".wav")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path,
             "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le", wav_path],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            log.error(f"ffmpeg: {r.stderr.decode()[:200]}")
            return None
        return wav_path
    except FileNotFoundError:
        log.error("ffmpeg not found — apt-get install -y ffmpeg")
        return None
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out")
        return None


def _cleanup(*paths: str):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _bytes_to_playable_wav(audio_bytes: bytes, is_wav: bool, label: str) -> str | None:
    """
    Write audio bytes to a temp file and return the path of a playable WAV.
    If is_wav=True  → write .wav directly (Sarvam output is already 8kHz WAV)
    If is_wav=False → write .mp3, convert via ffmpeg
    """
    if is_wav:
        wav_path = f"{TMP_DIR}/{label}.wav"
        with open(wav_path, "wb") as f:
            f.write(audio_bytes)
        return wav_path
    else:
        mp3_path = f"{TMP_DIR}/{label}.mp3"
        with open(mp3_path, "wb") as f:
            f.write(audio_bytes)
        wav_path = _mp3_to_wav(mp3_path)
        _cleanup(mp3_path)
        return wav_path


def _warm_audio_cache():
    """Pre-generate menu + greeting WAVs at startup — no latency on first call."""
    log.info("Warming audio cache…")

    lang_code = LANGUAGES[DEFAULT_LANG_KEY]["lang_code"]
    speaker   = LANGUAGES[DEFAULT_LANG_KEY]["sarvam_spk"]

    audio, is_wav = _synthesize(MENU_TEXT, lang_code, speaker)
    if audio:
        wav = _bytes_to_playable_wav(audio, is_wav, "menu_cache")
        if wav:
            _audio_cache["menu"] = wav
            log.info(f"  menu ready: {wav}")

    for key, lang in LANGUAGES.items():
        audio, is_wav = _synthesize(lang["greeting"], lang["lang_code"], lang["sarvam_spk"])
        if audio:
            wav = _bytes_to_playable_wav(audio, is_wav, f"greeting_cache_{key}")
            if wav:
                _audio_cache[f"greeting_{key}"] = wav
                log.info(f"  greeting_{key} ({lang['name']}) ready")

    log.info("Audio cache ready.")


# ── ESL helpers ───────────────────────────────────────────────────────────────

def _recv_raw(sock: socket.socket) -> str:
    buf = b""
    while not buf.endswith(b"\n\n"):
        try:
            chunk = sock.recv(4096)
        except OSError:
            return ""
        if not chunk:
            return ""
        buf += chunk
    return buf.decode("utf-8", errors="replace")


def _send(sock: socket.socket, data: str):
    try:
        sock.sendall((data + "\n\n").encode())
    except OSError:
        pass


def _execute(sock: socket.socket, app: str, arg: str = "") -> str:
    _send(sock, f"sendmsg\nCall-Command: execute\n"
                f"Execute-App-Name: {app}\nExecute-App-Arg: {arg}\n")
    return _recv_raw(sock)


def _api(sock: socket.socket, cmd: str) -> str:
    _send(sock, f"api {cmd}")
    return _recv_raw(sock)


def _uuid_from_block(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("Channel-Unique-ID:"):
            return line.split(":", 1)[1].strip()
    return None


def _hangup_cause(block: str) -> str:
    for line in block.splitlines():
        if line.startswith("Hangup-Cause:"):
            return line.split(":", 1)[1].strip()
    return "UNKNOWN"


# ── Event reader + queue ──────────────────────────────────────────────────────

_HANGUP_SENTINEL = "__HANGUP__"


def _event_reader(sock: socket.socket, ev_queue: queue.Queue, stop: threading.Event):
    while not stop.is_set():
        try:
            sock.settimeout(1.0)
            block = _recv_raw(sock)
        except OSError:
            ev_queue.put(_HANGUP_SENTINEL)
            return
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass
        if not block:
            ev_queue.put(_HANGUP_SENTINEL)
            return
        ev_queue.put(block)


def _wait_for_any(ev_queue: queue.Queue, events: list[str], timeout: float) -> str:
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return "TIMEOUT"
        try:
            block = ev_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if block is _HANGUP_SENTINEL or "Event-Name: CHANNEL_HANGUP" in block:
            return "CHANNEL_HANGUP"
        for ev in events:
            if f"Event-Name: {ev}" in block:
                return ev


def _wait_for_dtmf(ev_queue: queue.Queue, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            block = ev_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if block is _HANGUP_SENTINEL or "CHANNEL_HANGUP" in block:
            return None
        if "Event-Name: DTMF" in block:
            for line in block.splitlines():
                if line.startswith("DTMF-Digit:"):
                    return line.split(":", 1)[1].strip()


# ── Playback with barge-in ────────────────────────────────────────────────────

_BARGE_EVENTS = ["DTMF", "DETECTED_SPEECH"]


def _play_wav(
    sock: socket.socket,
    call_uuid: str,
    wav_path: str,
    ev_queue: queue.Queue,
    timeout: float = 30.0,
) -> bool:
    """Play WAV. Returns True=finished, False=barge-in/hangup."""
    _execute(sock, "playback", wav_path)
    event = _wait_for_any(
        ev_queue, ["CHANNEL_EXECUTE_COMPLETE"] + _BARGE_EVENTS, timeout=timeout,
    )
    if event in _BARGE_EVENTS:
        log.info(f"Barge-in ({event})")
        _api(sock, f"uuid_break {call_uuid} all")
        _wait_for_any(ev_queue, ["CHANNEL_EXECUTE_COMPLETE"], timeout=3.0)
        return False
    return event == "CHANNEL_EXECUTE_COMPLETE"


# ── Per-call handler ──────────────────────────────────────────────────────────

def handle_call(sock: socket.socket, addr):
    session_id = str(uuid.uuid4())
    log.info(f"New call from {addr} → session {session_id}")

    block = _recv_raw(sock)
    call_uuid = _uuid_from_block(block)
    log.info(f"Call UUID: {call_uuid}")

    _send(sock, "connect");  _recv_raw(sock)
    _send(sock, "myevents"); _recv_raw(sock)

    ev_queue: queue.Queue = queue.Queue()
    stop_reader = threading.Event()
    threading.Thread(
        target=_event_reader, args=(sock, ev_queue, stop_reader),
        name=f"reader-{call_uuid[:8]}", daemon=True,
    ).start()

    _execute(sock, "answer")
    time.sleep(0.5)
    _execute(sock, "set", "RECORD_SAMPLE_RATE=16000")
    _execute(sock, "set", "vad_energy_level=300")
    _execute(sock, "set", "vad_talk_hits=5")
    _execute(sock, "vad_test", "aleg")

    # ── Language menu ─────────────────────────────────────────────────────────
    _execute(sock, "set", "playback_terminators=any")
    menu_wav = _audio_cache.get("menu", FALLBACK_WAV)
    _play_wav(sock, call_uuid, menu_wav, ev_queue, timeout=15)

    digit    = _wait_for_dtmf(ev_queue, timeout=8)
    lang_key = digit if digit in LANGUAGES else DEFAULT_LANG_KEY
    lang     = LANGUAGES[lang_key]
    log.info(f"Language: {lang['name']} (digit={digit!r})")

    _execute(sock, "set", "playback_terminators=none")

    # ── Greeting ──────────────────────────────────────────────────────────────
    greeting_wav = _audio_cache.get(f"greeting_{lang_key}")
    if not greeting_wav:
        audio, is_wav = _synthesize(lang["greeting"], lang["lang_code"], lang["sarvam_spk"])
        if audio:
            greeting_wav = _bytes_to_playable_wav(audio, is_wav, f"greet_{session_id}")

    if greeting_wav:
        _play_wav(sock, call_uuid, greeting_wav, ev_queue, timeout=15)
        if f"greeting_{lang_key}" not in _audio_cache:
            _cleanup(greeting_wav)
    else:
        _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)

    # ── Conversation loop ─────────────────────────────────────────────────────
    stats         = CallStats()
    hangup_c      = "NORMAL_CLEARING"
    turn          = 0
    cache_hits    = 0
    prev_key: str | None = None   # Phase 3: last matched IVR phrase key

    use_streaming_stt = (STT_PROVIDER == "sarvam" and bool(SARVAM_API_KEY))

    while True:
        turn += 1
        timer = TurnTimer()

        # ── Phase 3: predict next response before recording ─────────────────
        predicted_key = ivr_cache.next_step(prev_key) if prev_key else None
        predicted_wav = ivr_cache.get_wav(predicted_key, lang_key) if predicted_key else None

        # ── 1. Record / stream ───────────────────────────────────────────────
        if use_streaming_stt:
            # Phase 1: FIFO streaming — no silence polling wait (~50ms VAD)
            fifo = f"{TMP_DIR}/audio_{session_id}_{turn}.raw"
            os.mkfifo(fifo)
            _execute(sock, "record", f"{fifo} {RECORD_MAX_SEC} 0 0")
            log.info(f"Turn {turn}: streaming STT via FIFO…")
            transcript = asyncio.run(
                _sarvam_streaming_stt(fifo, lang["lang_code"], call_uuid, sock)
            )
            _cleanup(fifo)
            # Wait for RECORD_STOP (uuid_break inside streaming STT triggers it)
            ev = _wait_for_any(ev_queue, ["RECORD_STOP"], timeout=5)
            if ev == "CHANNEL_HANGUP":
                hangup_c = ev
                break
            timer.mark("silence")   # ~50ms (Sarvam VAD)
            timer.mark("stt")       # already included in streaming_stt
        else:
            # Fallback: file-based recording with silence detection
            rec_path = f"{TMP_DIR}/rec_{session_id}_{turn}.wav"
            _execute(sock, "record",
                     f"{rec_path} {RECORD_MAX_SEC} 200 {int(RECORD_SILENCE_SEC)}")
            log.info(f"Turn {turn}: recording…")
            ev = _wait_for_any(ev_queue, ["RECORD_STOP"], timeout=RECORD_MAX_SEC + 5)
            if ev != "RECORD_STOP":
                hangup_c = ev
                log.info(f"Turn {turn}: {ev} — ending session")
                break
            timer.mark("silence")

            try:
                size = os.path.getsize(rec_path)
            except OSError:
                size = 0
            if size < 9600:
                log.info(f"Turn {turn}: too short ({size}B), skipping")
                _cleanup(rec_path)
                continue

            log.info(f"Turn {turn}: STT ({STT_PROVIDER})…")
            transcript = _transcribe(rec_path, lang["lang_code"])
            _cleanup(rec_path)
            timer.mark("stt")

        if not transcript:
            log.warning(f"Turn {turn}: empty transcript")
            not_heard = ivr_cache.get_wav("not_heard", lang_key) or FALLBACK_WAV
            _play_wav(sock, call_uuid, not_heard, ev_queue, timeout=10)
            continue

        log.info(f"Turn {turn}: '{transcript}'")

        # ── Phase 3: serve predicted phrase (skip LLM + TTS) ────────────────
        if predicted_wav:
            log.info(f"Turn {turn}: cache HIT (predicted={predicted_key}) — skip LLM+TTS")
            timer.mark("llm")
            timer.mark("tts")
            _play_wav(sock, call_uuid, predicted_wav, ev_queue, timeout=30)
            cache_hits += 1
            prev_key = predicted_key
            total = timer.total_ms()
            timer.log_extra(session_id, turn, lang["lang_code"], cache="hit")
            stats.add(total)
            continue

        # ── 2. LLM — try streaming first (Phase 2), fall back to sync ────────
        log.info(f"Turn {turn}: LLM streaming…")
        stream_futures = _chat_streaming(
            transcript, session_id, lang["lang_code"], lang["sarvam_spk"]
        )

        if stream_futures:
            timer.mark("llm")
            played = False
            for i, (sentence, fut) in enumerate(stream_futures):
                try:
                    wav_bytes = fut.result(timeout=10)
                except Exception as exc:
                    log.warning(f"Turn {turn} TTS future {i} failed: {exc}")
                    continue
                if not wav_bytes:
                    continue
                wav_path = _bytes_to_playable_wav(
                    wav_bytes, True, f"resp_{session_id}_{turn}_{i}"
                )
                if wav_path:
                    if i == 0:
                        timer.mark("tts")   # first sentence ready
                    _play_wav(sock, call_uuid, wav_path, ev_queue, timeout=30)
                    _cleanup(wav_path)
                    played = True
            if not played:
                _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)
                prev_key = None
                continue
            # Reconstruct full response text for cache matching
            full_answer = " ".join(s for s, _ in stream_futures)
        else:
            # Sync fallback
            log.info(f"Turn {turn}: LLM sync fallback…")
            mp3_bytes = _chat(transcript, session_id, lang["lang_code"], lang["sarvam_spk"])
            timer.mark("llm")
            if mp3_bytes is None:
                log.warning(f"Turn {turn}: gateway failed — fallback audio")
                _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)
                prev_key = None
                continue
            is_sarvam_tts = (TTS_PROVIDER == "sarvam")
            wav_path = _bytes_to_playable_wav(
                mp3_bytes, is_sarvam_tts, f"resp_{session_id}_{turn}"
            )
            timer.mark("tts")
            if wav_path is None:
                log.error(f"Turn {turn}: audio conversion failed")
                prev_key = None
                continue
            _play_wav(sock, call_uuid, wav_path, ev_queue, timeout=30)
            _cleanup(wav_path)
            # For sync path, we don't have the text — skip cache matching
            full_answer = ""

        # ── Phase 3: match LLM response to IVR cache for next turn ──────────
        if full_answer:
            matched_key, _ = ivr_cache.lookup(full_answer, lang_key)
            if matched_key:
                log.info(f"Turn {turn}: response matched cache key '{matched_key}'")
            prev_key = matched_key
        else:
            prev_key = None

        total = timer.total_ms()
        timer.log_extra(session_id, turn, lang["lang_code"], cache="miss")
        stats.add(total)

    stats.log_summary(session_id, lang["lang_code"], hangup_c, cache_hits, turn)
    stop_reader.set()
    sock.close()
    log.info(f"Session {session_id} ended — {turn} turns")


# ── Main ──────────────────────────────────────────────────────────────────────

def serve():
    _warm_audio_cache()
    # Phase 3: pre-TTS all IVR phrases
    ivr_cache.init(_tts_sarvam, TMP_DIR)
    ivr_cache.warm_cache(
        lang_keys=list(LANGUAGES.keys()),
        lang_configs={k: {"lang_code": v["lang_code"], "sarvam_spk": v["sarvam_spk"]}
                      for k, v in LANGUAGES.items()},
    )
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BRIDGE_HOST, BRIDGE_PORT))
    srv.listen(20)
    log.info(
        f"Sanjana bridge on {BRIDGE_HOST}:{BRIDGE_PORT} | "
        f"STT={STT_PROVIDER} TTS={TTS_PROVIDER}"
    )
    while True:
        try:
            conn, addr = srv.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(
            target=handle_call, args=(conn, addr),
            name=f"call-{addr[1]}", daemon=True,
        ).start()


if __name__ == "__main__":
    serve()

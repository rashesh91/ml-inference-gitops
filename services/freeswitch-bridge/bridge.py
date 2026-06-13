#!/usr/bin/env python3
"""
Sanjana FreeSWITCH AI Bridge — v3
----------------------------------
Improvements over v2:
  - HTTP instead of per-turn WebSocket  (simpler, no open/close overhead)
  - DTMF language menu at call start    (1=Hindi 2=English 3=Gujarati)

HTTP flow per turn:
  1. Record WAV   (FreeSWITCH record app)
  2. POST WAV  →  /stt/transcribe       (Whisper STT)
  3. POST text →  /api/chat             (LLM + TTS, returns audio_b64)
  4. ffmpeg MP3 → WAV, play with barge-in support

v2 fixes retained:
  Fix 1 – MP3 → WAV via ffmpeg (no mod_shout)
  Fix 2 – fallback sound on gateway error + 1 retry
  Fix 3 – barge-in (event-reader thread + uuid_break)
"""
import base64
import logging
import os
import queue
import socket
import subprocess
import threading
import time
import uuid

import requests

# ── Config ────────────────────────────────────────────────────────────────────
BRIDGE_HOST        = "0.0.0.0"
BRIDGE_PORT        = int(os.getenv("BRIDGE_PORT",        "8086"))
GATEWAY_BASE       = os.getenv("GATEWAY_BASE",           "https://aitest.lintel.in")
RECORD_SILENCE_SEC = int(os.getenv("RECORD_SILENCE_SEC", "2"))
RECORD_MAX_SEC     = int(os.getenv("RECORD_MAX_SEC",     "15"))
TMP_DIR            = os.getenv("TMP_DIR",                "/tmp/sanjana")
GATEWAY_RETRY_MAX  = 1
FALLBACK_WAV       = os.getenv(
    "FALLBACK_WAV",
    "/usr/share/freeswitch/sounds/en/us/callie/ivr/8000/ivr-please_try_again.wav",
)

# ── Language menu ─────────────────────────────────────────────────────────────
MENU_TEXT = (
    "Welcome to Symphony customer care. "
    "Hindi ke liye 1 dabaye. "
    "For English press 2. "
    "Gujarati mate 3 dabavo."
)

LANGUAGES = {
    "1": {
        "name":     "Hindi",
        "greeting": "Namaskar, main Sanjana Symphony customer care se. "
                    "Kya sahayata kar sakti hu?",
        "lang_hint": "hi",
    },
    "2": {
        "name":     "English",
        "greeting": "Hello, I'm Sanjana from Symphony customer care. "
                    "How may I help you?",
        "lang_hint": "en",
    },
    "3": {
        "name":     "Gujarati",
        "greeting": "Namaskar, hu Sanjana Symphony customer care mathi. "
                    "Shu madad kari shaku?",
        "lang_hint": "gu",
    },
}
DEFAULT_LANG_KEY = "2"   # English if no DTMF pressed

# ── Startup cache ─────────────────────────────────────────────────────────────
# Menu and greeting WAVs are generated once at bridge start so the first
# caller never waits for TTS. Populated by _warm_audio_cache().
_audio_cache: dict[str, str] = {}   # key → absolute WAV path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("bridge")

os.makedirs(TMP_DIR, exist_ok=True)


# ── FIX 1: MP3 → WAV ─────────────────────────────────────────────────────────

def _mp3_to_wav(mp3_path: str) -> str | None:
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


def _cleanup(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_stt(wav_path: str) -> str | None:
    """POST WAV → Whisper STT, return transcript or None."""
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
        log.error(f"STT error: {e}")
        return None


def _http_tts(text: str) -> bytes | None:
    """POST text → TTS service, return MP3 bytes or None."""
    try:
        resp = requests.post(
            f"{GATEWAY_BASE}/tts/synthesize",
            json={"text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.content or None
    except Exception as e:
        log.error(f"TTS error: {e}")
        return None


def _http_chat(text: str, session_id: str) -> bytes | None:
    """POST text + session_id → /api/chat, return MP3 bytes or None."""
    for attempt in range(1, GATEWAY_RETRY_MAX + 2):
        try:
            resp = requests.post(
                f"{GATEWAY_BASE}/api/chat",
                json={"text": text, "session_id": session_id},
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()
            b64 = data.get("audio_b64", "")
            return base64.b64decode(b64) if b64 else None
        except Exception as e:
            log.warning(f"Chat attempt {attempt}: {e}")
            if attempt <= GATEWAY_RETRY_MAX:
                time.sleep(0.5)
    return None


# ── TTS → cached WAV ──────────────────────────────────────────────────────────

def _tts_to_wav(text: str, label: str) -> str | None:
    """Synthesize text, convert MP3 → WAV, return WAV path or None."""
    mp3_bytes = _http_tts(text)
    if not mp3_bytes:
        return None
    mp3_path = f"{TMP_DIR}/{label}.mp3"
    with open(mp3_path, "wb") as f:
        f.write(mp3_bytes)
    wav_path = _mp3_to_wav(mp3_path)
    _cleanup(mp3_path)
    return wav_path


def _warm_audio_cache():
    """Pre-generate menu + greeting WAVs so first caller doesn't wait."""
    log.info("Warming audio cache…")

    wav = _tts_to_wav(MENU_TEXT, "menu")
    if wav:
        _audio_cache["menu"] = wav
        log.info(f"  menu WAV ready: {wav}")
    else:
        log.warning("  menu TTS failed — will use fallback sound")

    for key, lang in LANGUAGES.items():
        wav = _tts_to_wav(lang["greeting"], f"greeting_{key}")
        if wav:
            _audio_cache[f"greeting_{key}"] = wav
            log.info(f"  greeting_{key} ({lang['name']}) ready")
        else:
            log.warning(f"  greeting_{key} TTS failed")

    log.info("Audio cache warm.")


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
    cmd = (
        f"sendmsg\n"
        f"Call-Command: execute\n"
        f"Execute-App-Name: {app}\n"
        f"Execute-App-Arg: {arg}\n"
    )
    _send(sock, cmd)
    return _recv_raw(sock)


def _api(sock: socket.socket, cmd: str) -> str:
    _send(sock, f"api {cmd}")
    return _recv_raw(sock)


def _uuid_from_block(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("Channel-Unique-ID:"):
            return line.split(":", 1)[1].strip()
    return None


# ── Event reader + queue helpers ──────────────────────────────────────────────

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
    """Wait for one of `events`. Returns event name, CHANNEL_HANGUP, or TIMEOUT."""
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


def _extract_dtmf_digit(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("DTMF-Digit:"):
            return line.split(":", 1)[1].strip()
    return None


def _wait_for_dtmf(ev_queue: queue.Queue, timeout: float) -> str | None:
    """Wait for a DTMF digit. Returns digit char or None on timeout/hangup."""
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
            digit = _extract_dtmf_digit(block)
            if digit:
                return digit


# ── Playback with barge-in ────────────────────────────────────────────────────

_BARGE_EVENTS = ["DTMF", "DETECTED_SPEECH"]


def _play_wav(
    sock: socket.socket,
    call_uuid: str,
    wav_path: str,
    ev_queue: queue.Queue,
    timeout: float = 30.0,
) -> bool:
    """Play WAV. Returns True=finished normally, False=barge-in or hangup."""
    _execute(sock, "playback", wav_path)
    event = _wait_for_any(
        ev_queue,
        ["CHANNEL_EXECUTE_COMPLETE"] + _BARGE_EVENTS,
        timeout=timeout,
    )
    if event in _BARGE_EVENTS:
        log.info(f"Barge-in ({event}) — stopping playback")
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

    _send(sock, "connect")
    _recv_raw(sock)
    _send(sock, "myevents")
    _recv_raw(sock)

    ev_queue: queue.Queue = queue.Queue()
    stop_reader = threading.Event()
    threading.Thread(
        target=_event_reader,
        args=(sock, ev_queue, stop_reader),
        name=f"reader-{call_uuid[:8]}",
        daemon=True,
    ).start()

    _execute(sock, "answer")
    time.sleep(0.5)
    _execute(sock, "set", "RECORD_SAMPLE_RATE=16000")
    _execute(sock, "set", "vad_energy_level=300")
    _execute(sock, "set", "vad_talk_hits=5")
    _execute(sock, "vad_test", "aleg")

    # ── DTMF language menu ────────────────────────────────────────────────────
    # playback_terminators=any stops the menu as soon as a digit is pressed.
    _execute(sock, "set", "playback_terminators=any")

    menu_wav = _audio_cache.get("menu", FALLBACK_WAV)
    _play_wav(sock, call_uuid, menu_wav, ev_queue, timeout=15)

    # Collect DTMF: caller may press during or just after menu
    digit = _wait_for_dtmf(ev_queue, timeout=8)
    lang_key = digit if digit in LANGUAGES else DEFAULT_LANG_KEY
    lang = LANGUAGES[lang_key]
    log.info(f"Language selected: {lang['name']} (digit={digit!r})")

    # Disable playback_terminators for conversation (barge-in handled via event)
    _execute(sock, "set", "playback_terminators=none")

    # ── Greeting ──────────────────────────────────────────────────────────────
    greeting_wav = _audio_cache.get(f"greeting_{lang_key}")
    if not greeting_wav:
        greeting_wav = _tts_to_wav(lang["greeting"], f"greet_{session_id}")
    if greeting_wav:
        _play_wav(sock, call_uuid, greeting_wav, ev_queue, timeout=15)
        # Only unlink if it's a per-session file (not from cache)
        if f"greeting_{lang_key}" not in _audio_cache:
            _cleanup(greeting_wav)
    else:
        _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)

    # ── Conversation loop ─────────────────────────────────────────────────────
    turn = 0
    while True:
        turn += 1
        rec_path = f"{TMP_DIR}/rec_{session_id}_{turn}.wav"

        _execute(
            sock, "record",
            f"{rec_path} {RECORD_MAX_SEC} 200 {RECORD_SILENCE_SEC}",
        )
        log.info(f"Turn {turn}: recording…")

        event = _wait_for_any(ev_queue, ["RECORD_STOP"], timeout=RECORD_MAX_SEC + 5)
        if event != "RECORD_STOP":
            log.info(f"Turn {turn}: {event} — ending session")
            break

        try:
            size = os.path.getsize(rec_path)
        except OSError:
            size = 0

        if size < 9600:
            log.info(f"Turn {turn}: too short ({size}B), skipping")
            _cleanup(rec_path)
            continue

        # STT
        log.info(f"Turn {turn}: transcribing {size}B…")
        transcript = _http_stt(rec_path)
        _cleanup(rec_path)

        if not transcript:
            log.warning(f"Turn {turn}: STT returned empty")
            _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)
            continue

        log.info(f"Turn {turn}: transcript='{transcript}'")

        # LLM + TTS via /api/chat
        mp3_bytes = _http_chat(transcript, session_id)

        if mp3_bytes is None:
            log.warning(f"Turn {turn}: gateway failed — playing fallback")
            _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)
            continue

        # MP3 → WAV (Fix 1), play with barge-in (Fix 3)
        mp3_path = f"{TMP_DIR}/resp_{session_id}_{turn}.mp3"
        with open(mp3_path, "wb") as f:
            f.write(mp3_bytes)

        wav_path = _mp3_to_wav(mp3_path)
        _cleanup(mp3_path)

        if wav_path is None:
            log.error(f"Turn {turn}: ffmpeg failed")
            continue

        log.info(f"Turn {turn}: playing response")
        _play_wav(sock, call_uuid, wav_path, ev_queue, timeout=30)
        _cleanup(wav_path)

    stop_reader.set()
    sock.close()
    log.info(f"Session {session_id} ended — {turn} turns, lang={lang['name']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def serve():
    # Pre-generate menu + greeting audio before accepting calls
    _warm_audio_cache()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BRIDGE_HOST, BRIDGE_PORT))
    srv.listen(20)
    log.info(f"Sanjana bridge listening on {BRIDGE_HOST}:{BRIDGE_PORT}")

    while True:
        try:
            conn, addr = srv.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(
            target=handle_call,
            args=(conn, addr),
            name=f"call-{addr[1]}",
            daemon=True,
        ).start()


if __name__ == "__main__":
    serve()

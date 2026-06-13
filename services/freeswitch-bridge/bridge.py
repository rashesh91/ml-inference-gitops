#!/usr/bin/env python3
"""
Sanjana FreeSWITCH AI Bridge — v2 (production fixes)
-----------------------------------------------------
Fix 1: MP3 → WAV via ffmpeg   (no mod_shout dependency)
Fix 2: Fallback on gateway error  (play built-in error sound, retry once)
Fix 3: Barge-in  (caller speech or DTMF interrupts Sanjana mid-sentence)

Architecture change from v1:
  Each call spawns a background event-reader thread that drains the ESL socket
  into a queue.Queue. The main call thread reads from the queue. This lets us
  watch for barge-in events while also waiting for playback to finish — which
  was impossible in v1's sequential _recv_block design.
"""
import asyncio
import base64
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
import uuid

import websockets

# ── Config ────────────────────────────────────────────────────────────────────
BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8086"))
GATEWAY_WS   = os.getenv("GATEWAY_WS", "wss://aitest.lintel.in/ws")
GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    "Namaskar, main Sanjana Symphony customer care se. Kya sahayata kar sakti hu?"
)
RECORD_SILENCE_SEC = int(os.getenv("RECORD_SILENCE_SEC", "2"))
RECORD_MAX_SEC     = int(os.getenv("RECORD_MAX_SEC",     "15"))
TMP_DIR            = os.getenv("TMP_DIR", "/tmp/sanjana")

# FIX 2: built-in FreeSWITCH fallback sound (no TTS / gateway needed)
FALLBACK_WAV = os.getenv(
    "FALLBACK_WAV",
    "/usr/share/freeswitch/sounds/en/us/callie/ivr/8000/ivr-please_try_again.wav",
)
GATEWAY_RETRY_MAX = 1   # retry once on transient error before playing fallback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("bridge")

os.makedirs(TMP_DIR, exist_ok=True)


# ── FIX 1: MP3 → WAV conversion ──────────────────────────────────────────────

def _mp3_to_wav(mp3_path: str) -> str | None:
    """
    Convert MP3 to 8kHz mono PCM WAV using ffmpeg.
    Returns WAV path on success, None on failure.
    FreeSWITCH playback works natively with WAV — no mod_shout needed.
    """
    wav_path = mp3_path.replace(".mp3", ".wav")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_path,
                "-ar", "8000",        # 8 kHz — matches FS default telephony rate
                "-ac", "1",           # mono
                "-acodec", "pcm_s16le",
                wav_path,
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.error(f"ffmpeg failed: {result.stderr.decode()[:200]}")
            return None
        return wav_path
    except FileNotFoundError:
        log.error("ffmpeg not found — install with: apt-get install -y ffmpeg")
        return None
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out")
        return None


def _cleanup(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


# ── ESL raw socket helpers ────────────────────────────────────────────────────

def _recv_raw(sock: socket.socket) -> str:
    """Read one ESL block (ends with \\n\\n) from socket."""
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
    """Execute a dialplan app (non-blocking — reply arrives on event queue)."""
    cmd = (
        f"sendmsg\n"
        f"Call-Command: execute\n"
        f"Execute-App-Name: {app}\n"
        f"Execute-App-Arg: {arg}\n"
    )
    _send(sock, cmd)
    return _recv_raw(sock)   # read the immediate command ACK (not the event)


def _api(sock: socket.socket, cmd: str) -> str:
    """Send an ESL API command and read the reply."""
    _send(sock, f"api {cmd}")
    return _recv_raw(sock)


def _uuid_from_block(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("Channel-Unique-ID:"):
            return line.split(":", 1)[1].strip()
    return None


# ── Event reader thread ───────────────────────────────────────────────────────

# FIX 3 enabler: a background thread drains all incoming ESL events from the
# socket into a queue. The main call handler reads from the queue. This lets us
# detect barge-in (DTMF / DETECTED_SPEECH) while simultaneously waiting for
# playback to finish — impossible with v1's blocking _recv_block loop.

_HANGUP_SENTINEL = "__HANGUP__"


def _event_reader(sock: socket.socket, ev_queue: queue.Queue, stop: threading.Event):
    """Background thread: read ESL events → push to queue."""
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
    """
    Block until one of `events` fires, or timeout/hangup.
    Returns the matching event name, 'CHANNEL_HANGUP', or 'TIMEOUT'.
    """
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return "TIMEOUT"
        try:
            block = ev_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if block is _HANGUP_SENTINEL:
            return "CHANNEL_HANGUP"
        if "Event-Name: CHANNEL_HANGUP" in block:
            return "CHANNEL_HANGUP"
        for ev in events:
            if f"Event-Name: {ev}" in block:
                return ev
    # unreachable


# ── Voice Gateway ─────────────────────────────────────────────────────────────

async def _gateway_audio(session_id: str, wav_bytes: bytes) -> bytes | None:
    uri = f"{GATEWAY_WS}/{session_id}"
    try:
        async with websockets.connect(uri, open_timeout=10, close_timeout=5) as ws:
            await ws.send(json.dumps({
                "type": "audio",
                "data": base64.b64encode(wav_bytes).decode(),
            }))
            mp3_data = None
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t == "audio":
                    mp3_data = base64.b64decode(msg["data"])
                elif t == "done":
                    break
                elif t == "error":
                    log.error(f"Gateway error: {msg.get('message')}")
                    break
            return mp3_data
    except Exception as e:
        log.error(f"Gateway WS error: {e}")
        return None


async def _gateway_text(session_id: str, text: str) -> bytes | None:
    uri = f"{GATEWAY_WS}/{session_id}"
    try:
        async with websockets.connect(uri, open_timeout=10, close_timeout=5) as ws:
            await ws.send(json.dumps({"type": "text", "text": text}))
            mp3_data = None
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "audio":
                    mp3_data = base64.b64decode(msg["data"])
                elif t == "done":
                    break
            return mp3_data
    except Exception as e:
        log.error(f"Gateway text error: {e}")
        return None


def _call_gateway_audio(session_id: str, wav_bytes: bytes) -> bytes | None:
    return asyncio.run(_gateway_audio(session_id, wav_bytes))


def _call_gateway_text(session_id: str, text: str) -> bytes | None:
    return asyncio.run(_gateway_text(session_id, text))


# ── Playback with barge-in (FIX 3) ───────────────────────────────────────────

# FIX 3: BARGE-IN
# During playback we watch for two barge-in signals on the event queue:
#   DTMF         — caller pressed any key (common in IVR)
#   DETECTED_SPEECH — caller started speaking (fires if mod_vad / detect_speech active)
# Either one triggers uuid_break to immediately stop Sanjana's audio.
# After break, we drain the leftover CHANNEL_EXECUTE_COMPLETE and start recording.

_BARGE_IN_EVENTS = ["DTMF", "DETECTED_SPEECH"]


def _play_wav(
    sock: socket.socket,
    call_uuid: str,
    wav_path: str,
    ev_queue: queue.Queue,
    timeout: float = 30.0,
) -> bool:
    """
    Play a WAV file. Returns True on normal finish, False on barge-in or hangup.
    Barge-in: stops playback immediately and returns False so caller can re-record.
    """
    _execute(sock, "playback", wav_path)

    event = _wait_for_any(
        ev_queue,
        ["CHANNEL_EXECUTE_COMPLETE"] + _BARGE_IN_EVENTS,
        timeout=timeout,
    )

    if event in _BARGE_IN_EVENTS:
        log.info(f"Barge-in ({event}) — breaking playback")
        _api(sock, f"uuid_break {call_uuid} all")
        # drain the EXECUTE_COMPLETE that will follow the break
        _wait_for_any(ev_queue, ["CHANNEL_EXECUTE_COMPLETE"], timeout=3.0)
        return False

    if event == "CHANNEL_HANGUP":
        return False

    return True  # CHANNEL_EXECUTE_COMPLETE — normal finish


# ── Per-call handler ──────────────────────────────────────────────────────────

def handle_call(sock: socket.socket, addr):
    session_id = str(uuid.uuid4())
    log.info(f"New call from {addr} → session {session_id}")

    # ── ESL handshake ─────────────────────────────────────────────────────────
    block = _recv_raw(sock)
    call_uuid = _uuid_from_block(block)
    log.info(f"Call UUID: {call_uuid}")

    _send(sock, "connect")
    _recv_raw(sock)
    _send(sock, "myevents")
    _recv_raw(sock)

    # Start event reader background thread
    ev_queue: queue.Queue = queue.Queue()
    stop_reader = threading.Event()
    reader_thread = threading.Thread(
        target=_event_reader,
        args=(sock, ev_queue, stop_reader),
        name=f"reader-{call_uuid[:8]}",
        daemon=True,
    )
    reader_thread.start()

    # ── Answer ────────────────────────────────────────────────────────────────
    _execute(sock, "answer")
    time.sleep(0.5)

    # Force 16 kHz recording (better Whisper accuracy)
    _execute(sock, "set", "RECORD_SAMPLE_RATE=16000")

    # Enable VAD so DETECTED_SPEECH events fire for voice barge-in
    # (requires mod_vad in FreeSWITCH; silently no-ops if not loaded)
    _execute(sock, "set", "vad_energy_level=300")
    _execute(sock, "set", "vad_talk_hits=5")
    _execute(sock, "set", "vad_silence_hits=20")
    _execute(sock, "vad_test", "aleg")

    # ── Greeting ──────────────────────────────────────────────────────────────
    log.info("Fetching greeting audio…")
    greet_mp3 = _call_gateway_text(session_id, GREETING_TEXT)
    if greet_mp3:
        mp3_path = f"{TMP_DIR}/greet_{session_id}.mp3"
        with open(mp3_path, "wb") as f:
            f.write(greet_mp3)
        wav_path = _mp3_to_wav(mp3_path)   # FIX 1
        _cleanup(mp3_path)
        if wav_path:
            _play_wav(sock, call_uuid, wav_path, ev_queue, timeout=15)
            _cleanup(wav_path)
    else:
        # FIX 2: gateway down at greeting — play fallback and continue
        log.warning("Gateway unavailable for greeting, playing fallback")
        _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)

    # ── Conversation loop ─────────────────────────────────────────────────────
    turn = 0
    while True:
        turn += 1
        rec_path = f"{TMP_DIR}/rec_{session_id}_{turn}.wav"

        # Record caller utterance (stops on silence or max length)
        _execute(
            sock, "record",
            f"{rec_path} {RECORD_MAX_SEC} 200 {RECORD_SILENCE_SEC}",
        )
        log.info(f"Turn {turn}: recording…")

        event = _wait_for_any(ev_queue, ["RECORD_STOP"], timeout=RECORD_MAX_SEC + 5)
        if event != "RECORD_STOP":
            log.info(f"Turn {turn}: {event} — ending session")
            break

        # Skip recordings too short to contain speech (< ~0.3s of audio)
        try:
            size = os.path.getsize(rec_path)
        except OSError:
            size = 0
        if size < 9600:   # 0.3s × 8000 Hz × 2 bytes
            log.info(f"Turn {turn}: too short ({size}B), skipping")
            _cleanup(rec_path)
            continue

        # ── FIX 2: gateway call with retry ───────────────────────────────────
        log.info(f"Turn {turn}: sending {size}B to gateway…")
        with open(rec_path, "rb") as f:
            wav_bytes = f.read()
        _cleanup(rec_path)

        mp3_bytes = None
        for attempt in range(1, GATEWAY_RETRY_MAX + 2):
            mp3_bytes = _call_gateway_audio(session_id, wav_bytes)
            if mp3_bytes is not None:
                break
            log.warning(f"Turn {turn}: gateway attempt {attempt} failed")
            time.sleep(0.5)

        if mp3_bytes is None:
            # FIX 2: play "please try again" so caller is not left in silence
            log.warning(f"Turn {turn}: gateway down — playing fallback")
            _play_wav(sock, call_uuid, FALLBACK_WAV, ev_queue, timeout=10)
            continue

        # ── FIX 1: convert MP3 → WAV before playback ─────────────────────────
        mp3_path = f"{TMP_DIR}/resp_{session_id}_{turn}.mp3"
        with open(mp3_path, "wb") as f:
            f.write(mp3_bytes)

        wav_path = _mp3_to_wav(mp3_path)
        _cleanup(mp3_path)

        if wav_path is None:
            log.error(f"Turn {turn}: ffmpeg conversion failed, skipping playback")
            continue

        # ── FIX 3: play with barge-in support ────────────────────────────────
        log.info(f"Turn {turn}: playing response ({len(mp3_bytes)}B)")
        completed = _play_wav(sock, call_uuid, wav_path, ev_queue, timeout=30)
        _cleanup(wav_path)

        if not completed:
            # Caller interrupted — go straight to recording next utterance
            log.info(f"Turn {turn}: barge-in — re-recording immediately")
            # (loop continues, recording starts at top of next iteration)

    stop_reader.set()
    sock.close()
    log.info(f"Session {session_id} ended after {turn} turns")


# ── Main server ───────────────────────────────────────────────────────────────

def serve():
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

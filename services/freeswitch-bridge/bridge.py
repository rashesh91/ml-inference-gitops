#!/usr/bin/env python3
"""
Sanjana FreeSWITCH AI Bridge
-----------------------------
Outbound ESL socket server: FreeSWITCH connects here when a call arrives.

Flow per call:
  1. FreeSWITCH dials → TCP connection to port 8086
  2. Bridge answers → sets 16kHz recording
  3. Record caller audio (stops after 2s silence)
  4. Send WAV to voice-gateway WebSocket → get TTS response
  5. Play audio back to caller
  6. Loop until hangup
"""
import asyncio
import base64
import json
import logging
import os
import socket
import threading
import time
import uuid

import websockets

# ── Config ────────────────────────────────────────────────────────────────────
BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8086"))
GATEWAY_WS = os.getenv("GATEWAY_WS", "wss://aitest.lintel.in/ws")
GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    "Namaskar, main Sanjana Symphony customer care se. Kya sahayata kar sakti hu?"
)
RECORD_SILENCE_SEC = int(os.getenv("RECORD_SILENCE_SEC", "2"))   # stop after N seconds silence
RECORD_MAX_SEC = int(os.getenv("RECORD_MAX_SEC", "15"))          # max utterance length
TMP_DIR = os.getenv("TMP_DIR", "/tmp/sanjana")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("bridge")

os.makedirs(TMP_DIR, exist_ok=True)


# ── ESL raw socket helpers ────────────────────────────────────────────────────

def _recv_block(sock: socket.socket) -> str:
    """Read until double newline (one ESL message block)."""
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


def _execute(sock: socket.socket, app: str, arg: str = "", event_lock: bool = False) -> str:
    """Send a dialplan app execution command and read the reply."""
    cmd = (
        f"sendmsg\n"
        f"Call-Command: execute\n"
        f"Execute-App-Name: {app}\n"
        f"Execute-App-Arg: {arg}\n"
    )
    if event_lock:
        cmd += "Event-Lock: true\n"
    _send(sock, cmd)
    return _recv_block(sock)


def _uuid_from_block(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("Channel-Unique-ID:"):
            return line.split(":", 1)[1].strip()
    return None


def _wait_for_event(sock: socket.socket, event_name: str, timeout: float = 20.0) -> bool:
    """Drain events until we see event_name or timeout/hangup."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock.settimeout(deadline - time.time())
        try:
            block = _recv_block(sock)
        except OSError:
            return False
        finally:
            sock.settimeout(None)
        if not block:
            return False
        if f"Event-Name: {event_name}" in block:
            return True
        if "Event-Name: CHANNEL_HANGUP" in block:
            return False
    return False


# ── Voice Gateway WebSocket ───────────────────────────────────────────────────

async def _gateway_exchange(session_id: str, wav_bytes: bytes) -> bytes | None:
    """Send WAV audio to voice gateway, return MP3 audio bytes or None on error."""
    uri = f"{GATEWAY_WS}/{session_id}"
    try:
        async with websockets.connect(uri, open_timeout=10, close_timeout=5) as ws:
            b64 = base64.b64encode(wav_bytes).decode()
            await ws.send(json.dumps({"type": "audio", "data": b64}))

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
        log.error(f"Gateway WebSocket failed: {e}")
        return None


def _call_gateway(session_id: str, wav_bytes: bytes) -> bytes | None:
    """Synchronous wrapper — runs asyncio loop in this thread."""
    return asyncio.run(_gateway_exchange(session_id, wav_bytes))


# ── Greeting via gateway ──────────────────────────────────────────────────────

async def _gateway_text(session_id: str, text: str) -> bytes | None:
    """Send a text message to gateway (for greeting), return TTS audio."""
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
        log.error(f"Greeting gateway call failed: {e}")
        return None


# ── Per-call handler ──────────────────────────────────────────────────────────

def handle_call(sock: socket.socket, addr):
    session_id = str(uuid.uuid4())
    log.info(f"New call from {addr} → session {session_id}")

    # Read initial channel data block FreeSWITCH sends on connect
    block = _recv_block(sock)
    call_uuid = _uuid_from_block(block)
    log.info(f"Call UUID: {call_uuid}")

    # ESL handshake: tell FS we are ready
    _send(sock, "connect")
    _recv_block(sock)
    _send(sock, "myevents")
    _recv_block(sock)

    # Answer the call
    _execute(sock, "answer")
    time.sleep(0.5)

    # Force 16 kHz recording for better Whisper transcription
    _execute(sock, "set", "RECORD_SAMPLE_RATE=16000")

    # Get greeting audio from gateway and play it
    log.info("Fetching greeting audio…")
    greeting_audio = asyncio.run(_gateway_text(session_id, GREETING_TEXT))
    if greeting_audio:
        greeting_path = f"{TMP_DIR}/greet_{session_id}.mp3"
        with open(greeting_path, "wb") as f:
            f.write(greeting_audio)
        _execute(sock, "playback", greeting_path, event_lock=True)
        _wait_for_event(sock, "CHANNEL_EXECUTE_COMPLETE", timeout=15)
        try:
            os.unlink(greeting_path)
        except OSError:
            pass

    # Conversation loop
    turn = 0
    while True:
        turn += 1
        rec_path = f"{TMP_DIR}/rec_{session_id}_{turn}.wav"

        # Record caller utterance
        _execute(
            sock, "record",
            f"{rec_path} {RECORD_MAX_SEC} 200 {RECORD_SILENCE_SEC}",
            event_lock=False,
        )

        # Wait for recording to finish (silence timeout or max length)
        log.info(f"Turn {turn}: recording to {rec_path}…")
        stopped = _wait_for_event(sock, "RECORD_STOP", timeout=RECORD_MAX_SEC + 5)
        if not stopped:
            log.info(f"Turn {turn}: hangup detected, ending session")
            break

        # Skip empty/tiny recordings (< 0.5s = 16000 * 2 * 0.5 bytes header + ~32000)
        try:
            size = os.path.getsize(rec_path)
        except OSError:
            size = 0

        if size < 16000:
            log.info(f"Turn {turn}: recording too short ({size} bytes), skipping")
            try:
                os.unlink(rec_path)
            except OSError:
                pass
            continue

        # Send to voice gateway
        log.info(f"Turn {turn}: sending {size} bytes to gateway…")
        with open(rec_path, "rb") as f:
            wav_bytes = f.read()
        try:
            os.unlink(rec_path)
        except OSError:
            pass

        mp3_bytes = _call_gateway(session_id, wav_bytes)
        if mp3_bytes is None:
            log.warning(f"Turn {turn}: no response from gateway, continuing")
            continue

        # Play response
        resp_path = f"{TMP_DIR}/resp_{session_id}_{turn}.mp3"
        with open(resp_path, "wb") as f:
            f.write(mp3_bytes)

        log.info(f"Turn {turn}: playing {len(mp3_bytes)} byte response")
        _execute(sock, "playback", resp_path, event_lock=False)
        _wait_for_event(sock, "CHANNEL_EXECUTE_COMPLETE", timeout=30)
        try:
            os.unlink(resp_path)
        except OSError:
            pass

    sock.close()
    log.info(f"Session {session_id} ended after {turn} turns")


# ── Main ESL socket server ────────────────────────────────────────────────────

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
        t = threading.Thread(
            target=handle_call,
            args=(conn, addr),
            name=f"call-{addr[1]}",
            daemon=True,
        )
        t.start()


if __name__ == "__main__":
    serve()

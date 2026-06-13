# Sanjana Voice AI — Latency Analysis & Benchmarks

## Current Architecture Latency (v3 baseline)

```
Caller stops speaking
    │
    ├─ silence detection wait  ──  2000 ms  (RECORD_SILENCE_SEC=2)
    ├─ HTTP STT (Whisper/CPU)  ──   600 ms  (small model, CPU)
    ├─ HTTP /api/chat (LLM)    ──  1500 ms  (TinyLlama 1.1B, vLLM)
    ├─ HTTP TTS (gTTS)         ──   400 ms
    ├─ ffmpeg MP3→WAV          ──   120 ms
    └─ FreeSWITCH play setup   ──    50 ms
                                  ─────────
    TOTAL perceived latency    ──  4670 ms  (~4.7 seconds)
```

## Target for India IVR Market

| Tier | Latency | User Experience |
|------|---------|-----------------|
| Excellent | < 800 ms | Feels natural |
| Good | 800–1500 ms | Acceptable |
| Borderline | 1500–2500 ms | Noticeable pause |
| **Current** | **~4700 ms** | **Frustrating** |
| Bad | > 3000 ms | Callers hang up |

## Optimized Architecture (Sarvam AI)

```
Caller stops speaking
    │
    ├─ silence detection wait  ──   800 ms  (RECORD_SILENCE_SEC=0.8)
    ├─ Sarvam STT (saarika:v2) ──   250 ms  (cloud, Indian-optimized)
    ├─ HTTP /api/chat (LLM)    ──  1000 ms  (Llama 3.2 3B, GPU)
    ├─ Sarvam TTS (bulbul:v1)  ──   200 ms  (returns WAV directly)
    └─ FreeSWITCH play setup   ──    50 ms
                                  ─────────
    TOTAL perceived latency    ──  2300 ms  (~2.3 seconds)
```

## Per-Call Latency Log Format

The bridge logs a `LATENCY` line per turn and `CALL_SUMMARY` at end of call:

```
2026-06-13 11:00:01 INFO LATENCY session=abc123 turn=1 lang=hi \
  silence_ms=823 stt_ms=248 llm_ms=1043 tts_ms=198 ffmpeg_ms=0 total_ms=2312

2026-06-13 11:03:45 INFO CALL_SUMMARY session=abc123 turns=6 \
  avg_ms=2290 min_ms=1980 max_ms=2870 lang=hi hangup=NORMAL_CLEARING
```

## Extract Latency from Logs

```bash
# Average latency per turn over last 100 calls
journalctl -u sanjana-bridge --since "1 hour ago" \
  | grep LATENCY \
  | awk '{for(i=1;i<=NF;i++) if($i~/total_ms/) print $i}' \
  | cut -d= -f2 \
  | awk '{sum+=$1; n++} END {print "avg:", sum/n, "ms over", n, "turns"}'

# Per-stage breakdown
journalctl -u sanjana-bridge --since "1 hour ago" \
  | grep LATENCY \
  | awk '{
      for(i=1;i<=NF;i++) {
        if($i~/stt_ms/)    stt+= substr($i,8)+0
        if($i~/llm_ms/)    llm+= substr($i,8)+0
        if($i~/tts_ms/)    tts+= substr($i,8)+0
        if($i~/silence_ms/) sil+= substr($i,11)+0
        n++
      }
    } END {
      print "silence:", sil/n, "ms"
      print "stt:    ", stt/n, "ms"
      print "llm:    ", llm/n, "ms"
      print "tts:    ", tts/n, "ms"
    }'
```

## STT Comparison for Indian Languages

| Provider | Hindi | Gujarati | Tamil | Telugu | Latency | Cost |
|----------|-------|----------|-------|--------|---------|------|
| **Sarvam saarika:v2** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ~250ms | ₹ low |
| Google Cloud STT | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ~350ms | $$ |
| Azure Speech | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | ~300ms | $$ |
| Deepgram | ⭐⭐⭐ | ⭐⭐ | ⭐⭐ | ⭐⭐ | ~200ms | $$ |
| Whisper small (current) | ⭐⭐⭐ | ⭐⭐ | ⭐⭐ | ⭐⭐ | ~600ms | free |
| Bhashini (govt) | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ~500ms | free |

## TTS Comparison for Indian Languages

| Provider | Hindi | Gujarati | Naturalness | Latency | Cost |
|----------|-------|----------|-------------|---------|------|
| **Sarvam bulbul:v1** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | Very natural | ~200ms | ₹ low |
| Azure Neural | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Natural | ~250ms | $$ |
| Google WaveNet | ⭐⭐⭐⭐ | ⭐⭐⭐ | Good | ~300ms | $$ |
| ElevenLabs | ⭐⭐⭐⭐ | ⭐⭐ | Very natural | ~400ms | $$$ |
| gTTS (current) | ⭐⭐⭐ | ⭐⭐ | Robotic | ~400ms | free |
| Bhashini (govt) | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Good | ~500ms | free |

## Recommendation for Symphony IVR

**Use Sarvam AI** — Indian company, India-trained models, INR pricing:
- Sign up: https://dashboard.sarvam.ai
- STT: `saarika:v2` — best Hindi/Gujarati/Tamil accuracy
- TTS: `bulbul:v1` — most natural Indian voices

Set env vars:
```bash
STT_PROVIDER=sarvam
TTS_PROVIDER=sarvam
SARVAM_API_KEY=your_key_here
RECORD_SILENCE_SEC=1   # reduce from 2s to 1s
```

## Roadmap to < 1 second latency

1. **mod_audio_stream** — stream audio in real-time, eliminate silence wait entirely
2. **Partial STT** — start LLM as soon as STT returns high-confidence partial transcript
3. **Streaming LLM + TTS** — pipe first sentence from LLM directly to TTS before full response
4. Expected result: **800–1000 ms** end-to-end

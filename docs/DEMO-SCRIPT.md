# Demo Video Script — Voice Agentic AI Platform

**Target length:** 3 minutes  
**Format:** Screen recording + voiceover (no webcam needed)  
**Tools:** OBS Studio (free) or Loom  
**Resolution:** 1920×1080, 30fps

---

## Before You Record

### Setup checklist
- [ ] All services running: `docker-compose up` (check http://localhost:8000/health)
- [ ] Ollama model ready: `docker-compose exec llm-agent ollama pull mistral`
- [ ] Browser: Chrome or Firefox, microphone permission granted
- [ ] Close Slack, email, notifications — clean desktop
- [ ] Open these tabs in order (numbered so you can alt-tab cleanly):
  1. `http://localhost:8000` — Voice chat UI
  2. ArgoCD UI (if showing K8s — optional for 3-min version)
  3. `docker-compose logs -f` in a terminal (shows real-time pipeline logs)
- [ ] Do a silent test run first — make sure mic works, audio plays back
- [ ] Set browser zoom to 110% so UI text is readable in recording

### Practice the 4 demo questions out loud
1. "What time is it right now?"  
2. "What is the weather in Mumbai today?"  
3. "What is 15 percent of 2,450?"  
4. "Search for the latest news about artificial intelligence"

---

## Script (shot by shot)

---

### [0:00–0:15] Hook — Open on the final result

> **What you show:** The voice chat UI already open, one completed conversation visible.

**Voiceover (record this, don't read live):**
> "This is a voice AI agent I built from scratch — you speak, it thinks, it searches the web or does math if it needs to, then it speaks the answer back. Let me show you how it works, and then I'll walk through what's running under the hood."

*No typing. Just show the UI for 10 seconds, then move.*

---

### [0:15–0:30] First demo — Simple question (datetime tool)

> **What you show:** Click mic button, ask the question, watch the pipeline trace, hear the answer.

**Action:**
1. Click the purple mic button — it turns red with pulse animation
2. Say clearly: **"What time is it right now?"**
3. Click mic button again to stop
4. Watch the UI: status bar updates → transcript appears → response text appears → audio plays

**Voiceover (over the waiting animation):**
> "The browser sends the audio to a Whisper speech-to-text model. Whisper transcribes it, the LLM figures out it needs the datetime tool, calls it, and sends the answer to a text-to-speech service. The whole round trip takes about 2 seconds."

*Let the audio response play — don't talk over it.*

---

### [0:30–1:00] Second demo — Weather tool (shows external API)

> **What you show:** Ask the weather question, highlight the tool call badge that appears.

**Action:**
1. Click mic, say: **"What is the weather in Mumbai today?"**
2. Stop recording
3. When tool call badge appears (`🔧 weather({"location": "Mumbai"})`) — **pause your voiceover for 1 second so the badge is visible**
4. Audio response plays

**Voiceover (start when tool badge appears):**
> "Notice the tool call trace — the agent decided it needed live weather data, called the weather tool, got the result, then composed the spoken answer. The agent can chain multiple tools in a single turn if it needs to."

---

### [1:00–1:20] Third demo — Calculator (shows safe tool execution)

> **What you show:** Fast demo, show it's instant.

**Action:**
1. Type in the text box (to show text mode also works): **"What is 15 percent of 2450?"**
2. Hit Send
3. Show the near-instant calculator tool call badge + audio response

**Voiceover:**
> "The text input also works — useful for testing without a mic. The calculator runs inside a sandboxed Python AST evaluator so there's no code injection risk."

---

### [1:20–2:00] Architecture walkthrough — terminal + diagram

> **What you show:** Split screen — README architecture diagram on left, terminal logs on right.

**Switch to:** Terminal with `docker-compose logs -f`

**Voiceover (point to each service name as it appears in logs):**
> "There are four services. The voice gateway is the orchestrator — it handles the WebSocket connection from the browser, runs the ReAct agent loop, and coordinates the other two. Whisper STT runs on GPU in production, faster-whisper, handles transcription. The TTS service uses Microsoft Edge's neural voices — free, high quality, no GPU needed. And the LLM is Mistral 7B served through vLLM in production — here I'm using Ollama for local dev."

**Switch to:** README architecture diagram (or draw it live with ASCII in a text editor)

```
Browser → voice-gateway → whisper-stt  (GPU)
                        → llm-agent    (GPU + tools)
                        → tts-service  (no GPU)
```

**Voiceover:**
> "Each service is a separate FastAPI container. The gateway talks to all three over HTTP. This means you can scale the STT and TTS independently from the LLM."

---

### [2:00–2:30] Kubernetes + ArgoCD (the DevOps angle)

> **What you show:** ArgoCD UI showing all apps Synced + Healthy, then one Kubernetes command.

**Action:**
1. Open ArgoCD UI (or use a screenshot if not running K8s locally)
2. Show the app tree: `ml-infra-root` → namespaces → gpu-operator → monitoring → voice-platform
3. Switch to terminal, run:
```bash
kubectl get pods -n prod
```
Show all pods Running.

**Voiceover:**
> "The whole platform is deployed via GitOps. ArgoCD watches this Git repo — when I push a change, ArgoCD syncs it to the cluster automatically. The sync waves ensure namespaces and the GPU Operator are ready before the application pods start. I can roll back any deployment with a single git revert."

---

### [2:30–3:00] Closing — Stack summary

> **What you show:** Return to the voice UI. Do one final fast demo question.

**Action:**
1. Ask (via mic): **"Search for latest news about artificial intelligence"**
2. Watch search tool call badge appear
3. Hear the response

**Voiceover (over the final response):**
> "Stack: Python FastAPI, WebSockets, faster-whisper, vLLM with Mistral 7B, edge-tts, Kubernetes with NVIDIA GPU Operator, Helm, ArgoCD. The full source is on my GitHub — link in the description."

**End on the UI with the response visible. Fade out.**

---

## Post-Production (5 minutes of editing)

1. **Trim silence** — cut any pauses longer than 2 seconds
2. **Add captions** at these timestamps:
   - 0:15 `🎙 Whisper STT → Mistral 7B → Edge TTS`
   - 1:20 `⚙️ Architecture`
   - 2:00 `☸️ Kubernetes + ArgoCD`
3. **Background music** — optional, very quiet (Epidemic Sound: "Lo-fi focus" category)
4. **Thumbnail** — screenshot of the voice UI with tool call trace visible, add text overlay:
   `Voice AI Agent | Kubernetes + GPU | ArgoCD GitOps`

---

## YouTube / LinkedIn Description Template

```
Built a production-style Voice AI Agent platform from scratch.

🎙 Speak → Whisper STT (GPU) → Mistral 7B ReAct Agent → Edge TTS → Hear the answer

The agent can:
✅ Search the web (DuckDuckGo)
✅ Check live weather (wttr.in)  
✅ Solve math (sandboxed calculator)
✅ Tell time/date

Stack:
• Python FastAPI + WebSockets
• faster-whisper (OpenAI Whisper, GPU)
• vLLM serving Mistral 7B Instruct
• Edge TTS (Microsoft neural voices)
• Kubernetes with NVIDIA GPU Operator
• Helm charts + ArgoCD GitOps
• Multi-environment (dev/staging/prod) via ApplicationSets

Source code: github.com/rashesh91/ml-inference-gitops

#Kubernetes #AI #MLOps #ArgoCD #VoiceAI #Python #GPU
```

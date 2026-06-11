# ml-inference-gitops — Voice Agentic AI Platform

[![GitHub](https://img.shields.io/badge/GitHub-rashesh91%2Fml--inference--gitops-181717?logo=github)](https://github.com/rashesh91/ml-inference-gitops)
[![Live Demo](https://img.shields.io/badge/Live%20Demo-voice.rashesh.dev-brightgreen)](https://voice.rashesh.dev)
[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-GPU--aware-326CE5)](https://kubernetes.io)
[![ArgoCD](https://img.shields.io/badge/ArgoCD-GitOps-EF7B4D)](https://argoproj.github.io/cd)
[![Whisper](https://img.shields.io/badge/Whisper-STT-green)](https://github.com/openai/whisper)
[![vLLM](https://img.shields.io/badge/vLLM-Mistral--7B-purple)](https://vllm.ai)

A production-grade **Voice AI Agent** running on Kubernetes, managed via ArgoCD GitOps.

**Speak → Whisper (STT) → Mistral (ReAct Agent + Tools) → Edge TTS → Hear the answer**

---

## Architecture

```
Browser
  │  WebSocket (base64 audio)
  ▼
voice-gateway  ─── HTTP ───▶  whisper-stt   (GPU: faster-whisper)
  │                                │ transcript
  │            ─── HTTP ───▶  llm-agent     (GPU: vLLM + Mistral 7B)
  │                                │ ReAct loop
  │                           ┌────┴────────────────────┐
  │                           │  Tools (no GPU needed)  │
  │                           │  • search (DuckDuckGo)  │
  │                           │  • weather (wttr.in)    │
  │                           │  • calculator (safe)    │
  │                           │  • get_datetime         │
  │                           └─────────────────────────┘
  │            ─── HTTP ───▶  tts-service   (edge-tts, no GPU)
  │                                │ MP3 audio
  ▼
Browser plays audio response
```

---

## Minimum Server Requirements

### Local Development (Docker Compose, no GPU)

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 4 cores | 8 cores |
| RAM | 10 GB | 16 GB |
| Disk | 15 GB free | 30 GB free |
| OS | Linux / macOS / Windows (WSL2) | Ubuntu 22.04 |
| Docker | v24+ | v24+ |
| Internet | Required (edge-tts calls Microsoft API) | Stable broadband |

> Uses Ollama + `mistral:7b-q4_0` (4-bit quantized). Inference is slower on CPU (~10–20s/response) but fully functional.
> Switch to `tinyllama` for faster responses on 8 GB RAM machines.

---

### Portfolio VPS (Single Server, CPU-only)

Runs the full stack on one cheap cloud VM.

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 6 GB | 8 GB |
| Disk | 20 GB SSD | 40 GB SSD |
| Network | 100 Mbps | 200 Mbps |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Provider example | Hetzner CX22 — €3.90/mo | Hetzner CX32 — €7.90/mo |

---

### Production Kubernetes (GPU cluster)

| Node type | Count | CPU | RAM | GPU | Role |
|-----------|-------|-----|-----|-----|------|
| GPU node | 1–2 | 8 vCPU | 32 GB | 1× NVIDIA T4 (16 GB VRAM) or better | Whisper STT + vLLM |
| CPU node | 2 | 4 vCPU | 8 GB | — | Gateway, TTS, monitoring |
| **Total minimum** | **3 nodes** | **16 vCPU** | **48 GB** | **1× T4** | |

**Per-service resource breakdown:**

| Service | CPU request | RAM request | GPU |
|---------|------------|-------------|-----|
| `whisper-stt` | 2 cores | 4 GB | 1× GPU (optional, CPU fallback works) |
| `llm-agent` (vLLM) | 4 cores | 16 GB | 1× GPU required for Mistral 7B |
| `tts-service` | 0.25 cores | 256 MB | None |
| `voice-gateway` | 0.5 cores | 512 MB | None |
| Prometheus + Grafana | 1 core | 2 GB | None |
| ArgoCD | 1 core | 1 GB | None |

**GPU requirements by model:**

| Model | VRAM needed | CPU RAM fallback |
|-------|------------|-----------------|
| TinyLlama 1.1B | 2 GB VRAM | 4 GB RAM |
| Mistral 7B (Q4 quantized) | 6 GB VRAM | 8 GB RAM (slow) |
| Mistral 7B (full BF16) | 16 GB VRAM | not recommended |
| Llama 2 13B | 28 GB VRAM | not recommended |

---

## Services

| Service | Port | GPU | Purpose |
|---------|------|-----|---------|
| `voice-gateway` | 8000 | No | WebSocket orchestrator + web UI |
| `whisper-stt` | 8001 | Yes | Audio → transcript (faster-whisper) |
| `llm-agent` | 8000/11434 | Yes | ReAct agent (vLLM / Ollama) |
| `tts-service` | 8002 | No | Text → speech (edge-tts, free) |

---

## Quick Start — Local (Docker Compose, no GPU)

```bash
cd ml-inference-gitops

# Build and start all services
docker-compose up --build

# In a separate terminal, pull the LLM model (one-time, ~4GB)
docker-compose exec llm-agent ollama pull mistral

# Open the voice chat UI
open http://localhost:8000
```

That's it. Click the microphone and ask something like:
- _"What's the weather in Mumbai?"_
- _"What is 15 percent of 847?"_
- _"What time is it?"_
- _"Search for latest news about AI"_

---

## Kubernetes Deployment (GPU cluster, ArgoCD)

### Step 1 — Build and push images

```bash
REGISTRY=your-registry.io/voice-platform

docker build -t $REGISTRY/whisper-stt:latest services/whisper-stt/
docker build -t $REGISTRY/tts-service:latest  services/tts-service/
docker build -t $REGISTRY/voice-gateway:latest services/voice-gateway/
docker push $REGISTRY/whisper-stt:latest
docker push $REGISTRY/tts-service:latest
docker push $REGISTRY/voice-gateway:latest
```

### Step 2 — Update image registry in values.yaml

```yaml
# applications/voice-platform/values.yaml
global:
  imageRegistry: "your-registry.io/voice-platform/"
```

### Step 3 — Bootstrap the cluster

```bash
# Apply namespaces, RBAC, GPU Operator (see docs/QUICKSTART.md Phase 1-2)
kubectl apply -f infrastructure/namespaces/namespaces.yaml
kubectl apply -f infrastructure/rbac/

# Install ArgoCD
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Bootstrap App of Apps
kubectl apply -f argocd-config/projects.yaml
kubectl apply -f argocd-config/root-app.yaml -n argocd
```

### Step 4 — ArgoCD deploys everything

ArgoCD will sync in order (sync waves):
- Wave 0: Namespaces, RBAC
- Wave 1: GPU Operator
- Wave 2: Monitoring (Prometheus + Grafana)
- Wave 3: Voice platform (Whisper + LLM + TTS + Gateway) per environment

Watch progress: `kubectl get applications -n argocd -w`

Access the UI: `https://voice.example.com` (update `voiceGateway.ingress.host` in values.yaml)

---

## Project Structure

```
ml-inference-gitops/
├── services/                   ← Python microservice source code
│   ├── whisper-stt/            ← faster-whisper STT server
│   │   ├── app/main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── tts-service/            ← edge-tts synthesis server
│   │   ├── app/main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── voice-gateway/          ← Orchestrator + ReAct agent + web UI
│       ├── app/
│       │   ├── main.py         ← FastAPI WebSocket server
│       │   ├── pipeline.py     ← STT → Agent → TTS pipeline
│       │   ├── agent.py        ← ReAct agent loop
│       │   ├── config.py       ← Settings from env vars
│       │   └── tools/          ← Agent tools
│       │       ├── search.py   ← DuckDuckGo (no API key)
│       │       ├── weather.py  ← wttr.in (no API key)
│       │       ├── calculator.py ← safe AST eval
│       │       └── datetime_tool.py
│       ├── static/
│       │   ├── index.html      ← Voice chat UI
│       │   └── app.js          ← WebSocket + Audio API
│       ├── Dockerfile
│       └── requirements.txt
├── applications/
│   └── voice-platform/         ← Helm chart for K8s deployment
│       ├── Chart.yaml
│       ├── values.yaml         ← Base values + GPU config
│       └── templates/          ← K8s manifests per service
├── infrastructure/             ← Namespaces, RBAC, GPU Operator
├── argocd-config/              ← App of Apps + ApplicationSets
├── docker-compose.yml          ← Local dev (no K8s needed)
├── tests/                      ← E2E + GPU scheduling + rollback tests
└── docs/                       ← Quickstart + runbooks
```

---

## Agent Tools

| Tool | Description | API |
|------|-------------|-----|
| `search(query)` | Web search | DuckDuckGo (no key) |
| `weather(location)` | Current weather + forecast | wttr.in (no key) |
| `calculator(expr)` | Safe math evaluation | stdlib (sandboxed AST) |
| `get_datetime()` | Current date/time | stdlib |

Add new tools by creating a file in `services/voice-gateway/app/tools/` and registering it in `__init__.py`.

---

## WebSocket Protocol

```
Client → Server:
  {type: "audio",  data: "<base64 webm>"}   → triggers full voice pipeline
  {type: "text",   text: "..."}             → text-only (no mic needed)
  {type: "reset"}                            → clear session history

Server → Client:
  {type: "status",      message: "..."}
  {type: "transcript",  text: "..."}
  {type: "tool_call",   tool: "...", args: {...}}
  {type: "tool_result", tool: "...", result: "..."}
  {type: "response",    text: "..."}
  {type: "audio",       data: "<base64 mp3>"}
  {type: "done"}
  {type: "error",       message: "..."}
```

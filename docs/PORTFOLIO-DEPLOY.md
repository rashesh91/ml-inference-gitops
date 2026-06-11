# Portfolio Deployment Guide

Get a live, always-on demo at `https://voice.yourdomain.com` for **~$6/month**.

---

## Option A — VPS + Docker Compose (Recommended for portfolio)

Cheapest, easiest, no GPU required. Ollama runs Mistral on CPU (slower but works).

### Providers (pick one)

| Provider | Spec | Cost | Notes |
|----------|------|------|-------|
| **Hetzner CX22** | 2 vCPU, 4GB RAM | €3.90/mo | Best value, EU datacenters |
| **DigitalOcean Basic** | 2 vCPU, 2GB RAM | $12/mo | Easy UI, good docs |
| **Vultr Cloud Compute** | 1 vCPU, 2GB RAM | $6/mo | Global locations |
| **Contabo VPS S** | 4 vCPU, 8GB RAM | €4.99/mo | Best RAM for the price |

> Recommendation: **Hetzner CX22** (€3.90/mo, 4GB RAM handles Ollama + all services comfortably)

---

### Step 1 — Provision the server

```bash
# After creating your VPS, SSH in as root
ssh root@YOUR_SERVER_IP

# Update and install Docker
apt-get update && apt-get upgrade -y
curl -fsSL https://get.docker.com | sh
apt-get install -y docker-compose-plugin git

# Verify
docker --version
docker compose version
```

---

### Step 2 — Clone your repo

```bash
cd /opt
git clone https://github.com/rashesh91/ml-inference-gitops.git
cd ml-inference-gitops
```

---

### Step 3 — Configure environment

```bash
# Create .env file for docker-compose
cat > .env << 'EOF'
TTS_VOICE=en-US-JennyNeural
WHISPER_MODEL_SIZE=base
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
LLM_MODEL=mistral
EOF
```

---

### Step 4 — Set up HTTPS with Caddy (automatic TLS)

Caddy handles HTTPS automatically with Let's Encrypt. No certbot needed.

```bash
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

# Create Caddyfile
cat > /etc/caddy/Caddyfile << 'EOF'
voice.yourdomain.com {
    reverse_proxy localhost:8000 {
        # Required for WebSocket upgrade
        header_up Host {host}
        header_up X-Real-IP {remote_host}
        transport http {
            read_timeout 120s
            write_timeout 120s
        }
    }
}
EOF

systemctl enable caddy
systemctl restart caddy
```

> Replace `voice.yourdomain.com` with your actual subdomain.  
> Point your domain's A record to `YOUR_SERVER_IP` before this step.

---

### Step 5 — Start all services

```bash
cd /opt/ml-inference-gitops

# Build images (first time: ~10 minutes)
docker compose build

# Start in background
docker compose up -d

# Watch startup logs
docker compose logs -f --tail=50
```

---

### Step 6 — Pull the LLM model (one-time, ~4GB download)

```bash
# Wait for Ollama container to be healthy first
docker compose exec llm-agent ollama pull mistral

# Verify it loaded
docker compose exec llm-agent ollama list
```

---

### Step 7 — Verify everything is live

```bash
# Check all containers are running
docker compose ps

# Test each service
curl https://voice.yourdomain.com/health
curl http://localhost:8001/health   # whisper-stt
curl http://localhost:8002/health   # tts-service

# Open in browser
echo "Open: https://voice.yourdomain.com"
```

---

### Step 8 — Auto-restart on server reboot

```bash
# Create systemd service so docker-compose starts on boot
cat > /etc/systemd/system/voice-ai.service << 'EOF'
[Unit]
Description=Voice AI Agent Platform
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/opt/ml-inference-gitops
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl enable voice-ai
systemctl start voice-ai
```

---

### Step 9 — Auto-deploy on git push (optional but impressive)

```bash
# Install webhook tool
apt-get install -y webhook

# Create deploy script
cat > /opt/deploy.sh << 'EOF'
#!/bin/bash
cd /opt/ml-inference-gitops
git pull origin main
docker compose build voice-gateway tts-service whisper-stt
docker compose up -d --no-deps voice-gateway tts-service whisper-stt
EOF
chmod +x /opt/deploy.sh

# Webhook config
mkdir -p /opt/webhook
cat > /opt/webhook/hooks.json << 'EOF'
[{
  "id": "deploy-voice-ai",
  "execute-command": "/opt/deploy.sh",
  "command-working-directory": "/opt/ml-inference-gitops",
  "trigger-rule": {
    "match": {
      "type": "payload-hmac-sha256",
      "secret": "YOUR_WEBHOOK_SECRET",
      "parameter": { "source": "header", "name": "X-Hub-Signature-256" }
    }
  }
}]
EOF

# Add to Caddyfile (webhook endpoint)
cat >> /etc/caddy/Caddyfile << 'EOF'

voice.yourdomain.com/webhook {
    reverse_proxy localhost:9000
}
EOF
systemctl reload caddy

# Start webhook server
webhook -hooks /opt/webhook/hooks.json -port 9000 -hotreload &
```

Then in GitHub → repo Settings → Webhooks → add `https://voice.yourdomain.com/webhook` with your secret. Every `git push` to main auto-deploys.

---

## Option B — Free Tier Cloud (Google Cloud Run / Railway)

If you don't want to pay for a VPS, Railway.app can host docker-compose apps on a free tier (~$0–5/mo).

```bash
# Install Railway CLI
npm install -g @railway/cli
railway login

# Deploy from the repo root
cd /opt/ml-inference-gitops
railway init
railway up
```

**Limitation:** Free tier sleeps after 30 min of inactivity — not ideal for live demos. VPS is better.

---

## Option C — GPU VPS (for real GPU inference demo)

If you want to show the actual GPU Whisper + vLLM path:

| Provider | GPU | Cost |
|----------|-----|------|
| **Vast.ai** | RTX 3080 | ~$0.20/hr |
| **Lambda Labs** | A10 | $0.75/hr |
| **RunPod** | RTX 4090 | $0.34/hr |

Turn it on only during demo recording. Cost: ~$1–2 total for a recording session.

```bash
# On GPU VPS, edit docker-compose.yml:
# whisper-stt: WHISPER_DEVICE=cuda, WHISPER_COMPUTE_TYPE=float16
# Uncomment the GPU reservation block under whisper-stt

docker compose up -d
```

---

## Keeping Costs Low

| Tip | Saving |
|-----|--------|
| Use Hetzner over DigitalOcean | ~$8/mo savings |
| Use `mistral:7b-instruct-q4_0` (quantized) instead of full mistral | 50% less RAM, same quality |
| Set Ollama `OLLAMA_NUM_PARALLEL=1` | Prevents OOM on 4GB RAM |
| Caddy auto-renews TLS — no extra cert cost | $0 |
| Use Cloudflare free DNS + proxy for DDoS protection | $0 |

---

## Portfolio Checklist

Before sharing the link publicly:

- [ ] `https://voice.yourdomain.com` loads and mic works
- [ ] Test all 4 demo questions from DEMO-SCRIPT.md
- [ ] Add the live URL to your GitHub README badge:
  ```markdown
  [![Live Demo](https://img.shields.io/badge/Live%20Demo-voice.yourdomain.com-brightgreen)](https://voice.yourdomain.com)
  ```
- [ ] Add the URL to your LinkedIn "Featured" section
- [ ] Pin the repo on your GitHub profile
- [ ] Add to CV under "Projects": `Voice Agentic AI Platform | github.com/you/ml-inference-gitops`

---

## Troubleshooting

**"Model too slow on CPU"**
```bash
# Switch to a smaller model
docker compose exec llm-agent ollama pull tinyllama
# Update .env: LLM_MODEL=tinyllama
docker compose restart voice-gateway
```

**"Ollama runs out of memory"**
```bash
# Use quantized mistral (half the RAM, same quality)
docker compose exec llm-agent ollama pull mistral:7b-instruct-q4_0
```

**"WebSocket disconnects after 60s"**
```bash
# Already handled by Caddy config above (read_timeout 120s)
# If still happening, check your cloud provider's load balancer timeout settings
```

**"Audio doesn't play in Safari"**
Safari requires user gesture before audio — the mic button click counts. If still broken:
add `audio.load()` before `audio.play()` in `static/app.js:playAudioB64()`.

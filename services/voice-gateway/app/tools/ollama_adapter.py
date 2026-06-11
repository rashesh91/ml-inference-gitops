"""
Thin adapter: translates Ollama /api/chat to OpenAI /v1/chat/completions response format.
Used only in local dev when LLM_URL points to Ollama.
The voice-gateway agent.py calls /v1/chat/completions — this adapter is NOT needed
when pointing at vLLM (which is natively OpenAI-compatible).

To use Ollama locally:
  1. In docker-compose.yml: LLM_URL=http://llm-agent:11434
  2. Run: docker exec -it <ollama-container> ollama pull mistral
  3. Set LLM_MODEL=mistral in env

Ollama also supports the OpenAI-compatible endpoint at /v1/chat/completions
as of Ollama v0.1.24+, so this adapter may not be needed at all.
Just set LLM_URL=http://llm-agent:11434 and it works natively.
"""
# No code needed — Ollama v0.1.24+ natively serves /v1/chat/completions

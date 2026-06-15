from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    stt_url: str = "http://whisper-stt:8001"
    tts_url: str = "http://tts-service:8002"
    llm_url: str = "http://llm-agent:8000"      # vLLM OpenAI-compatible endpoint
    llm_model: str = "mistral-7b"
    tts_voice: str = "en-US-JennyNeural"
    max_history_turns: int = 10                  # per session
    agent_max_iterations: int = 5
    request_timeout: float = 60.0

    # Security & rate-limiting
    gateway_api_key: str = ""        # empty = dev mode (no auth enforced)
    rate_limit_rpm: int = 30         # max REST requests per IP per minute
    max_text_len: int = 1000         # max chars for text/transcript input
    max_audio_bytes: int = 5_242_880 # 5 MB max audio payload
    session_ttl_minutes: int = 60    # evict sessions idle longer than this

    class Config:
        env_file = ".env"


settings = Settings()

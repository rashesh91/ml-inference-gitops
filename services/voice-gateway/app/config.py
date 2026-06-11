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

    class Config:
        env_file = ".env"


settings = Settings()

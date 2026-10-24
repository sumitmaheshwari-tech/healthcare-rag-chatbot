"""Application configuration loaded from environment variables."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Ensure SSL certificate verification works reliably across all Windows Python environments
try:
    import pip_system_certs.wrapt_requests
except Exception:
    pass
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

# Load .env from project root with override enabled for hot-reloads
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)


class Settings:
    """Central configuration for the healthcare RAG chatbot."""

    # Provider setting: 'gemini', 'nvidia', 'openrouter', or 'ollama'
    _google_key: str = os.getenv("GOOGLE_API_KEY", "")
    _default_provider: str = "gemini" if _google_key.startswith("AIza") else "nvidia"
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", _default_provider).lower()

    # NVIDIA Build API (Primary — direct NVIDIA GPU cloud)
    _raw_nvidia_key: str = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_API_KEY: str = "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" if (not _raw_nvidia_key or "5J0y5km" in _raw_nvidia_key) else _raw_nvidia_key
    _raw_nvidia_model: str = os.getenv("NVIDIA_LLM_MODEL", "")
    NVIDIA_LLM_MODEL: str = "meta/llama-3.2-11b-vision-instruct" if (not _raw_nvidia_model or "nemotron" in _raw_nvidia_model or "3.1" in _raw_nvidia_model) else _raw_nvidia_model
    NVIDIA_EMBED_MODEL: str = os.getenv("NVIDIA_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")

    # Google Gemini API (Fallback LLM — ultra fast 1.3s)
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.0-flash")
    # Embeddings (NVIDIA Build / OpenRouter / Gemini / Local)
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b")

    # OpenRouter API (Secondary Fallback)
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3.5-lightning:free")

    # Ollama (Local)
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1")

    # ChromaDB
    _raw_chroma: str = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
    if (os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME")) and "/tmp" not in _raw_chroma:
        CHROMA_PERSIST_DIR: str = "/tmp/chroma_db"
    else:
        CHROMA_PERSIST_DIR: str = _raw_chroma
    CHROMA_COLLECTION: str = "hospital_knowledge"

    # Database — Ensure explicit postgresql+psycopg2 driver for SQLAlchemy 2.0
    _raw_db_url: str = os.getenv("DATABASE_URL", "sqlite:///./hospital.db")
    if (os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME")) and "sqlite" in _raw_db_url and "/tmp" not in _raw_db_url:
        _raw_db_url = "sqlite:////tmp/hospital.db"
    if _raw_db_url.startswith("postgres://"):
        DATABASE_URL: str = _raw_db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif _raw_db_url.startswith("postgresql://") and "+psycopg" not in _raw_db_url:
        DATABASE_URL: str = _raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    else:
        DATABASE_URL: str = _raw_db_url

    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent
    KNOWLEDGE_DIR: Path = BASE_DIR / "knowledge" / "documents"

    # Environment
    ENV: str = os.getenv("ENV", "development")  # development, staging, production

    # Security
    COOKIE_SECURE: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Cache TTLs (seconds)
    CACHE_EMBEDDING_TTL: int = int(os.getenv("CACHE_EMBEDDING_TTL", "3600"))  # 1 hour
    CACHE_RETRIEVAL_TTL: int = int(os.getenv("CACHE_RETRIEVAL_TTL", "300"))   # 5 min
    CACHE_RESPONSE_TTL: int = int(os.getenv("CACHE_RESPONSE_TTL", "600"))     # 10 min

    # Admin Notifications (Telegram & Email)
    _raw_admin_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_BOT_TOKEN: str = "8628227561:AAFc97utoaQWlr66Je-ExLaOyIoe5xzYAZo" if (not _raw_admin_token or "8662117383" in _raw_admin_token) else _raw_admin_token
    TELEGRAM_ADMIN_CHAT_ID: str = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "8608906450")
    SMTP_EMAIL: str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "")

    # Dedicated Telegram Patient Authentication Bot
    _raw_auth_token = os.getenv("TELEGRAM_AUTH_BOT_TOKEN", "")
    TELEGRAM_AUTH_BOT_TOKEN: str = "8910785925:AAHthVhUusbzj-xur5iN8glh-ud5bY20Kl4" if (not _raw_auth_token or "8630224222" in _raw_auth_token) else _raw_auth_token

    _raw_auth_user = os.getenv("TELEGRAM_AUTH_BOT_USERNAME", "")
    TELEGRAM_AUTH_BOT_USERNAME: str = "Medcare_Secure_Auth_bot" if (not _raw_auth_user or "MedCare_Verify_Auth_bot" in _raw_auth_user) else _raw_auth_user


settings = Settings()

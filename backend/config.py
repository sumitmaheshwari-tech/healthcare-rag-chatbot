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
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini").lower()

    # NVIDIA Build API (Primary — direct NVIDIA GPU cloud)
    NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_LLM_MODEL: str = os.getenv("NVIDIA_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
    NVIDIA_EMBED_MODEL: str = os.getenv("NVIDIA_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")

    # Google Gemini API (Fallback LLM — ultra fast 1.3s)
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")
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
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "8662117383:AAHl1cTXSYsrA2jraHSQW7ZWxQJzsfLJm1U")
    TELEGRAM_ADMIN_CHAT_ID: str = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "8608906450")
    SMTP_EMAIL: str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "")

    # Dedicated Telegram Patient Authentication Bot
    TELEGRAM_AUTH_BOT_TOKEN: str = os.getenv("TELEGRAM_AUTH_BOT_TOKEN", "8630224222:AAFqjxhqGmkuEbZeoeRLrh7M4A30fDD-4uc")
    TELEGRAM_AUTH_BOT_USERNAME: str = os.getenv("TELEGRAM_AUTH_BOT_USERNAME", "MedCare_Verify_Auth_bot")


settings = Settings()

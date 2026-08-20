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

    # Provider setting: 'groq', 'ollama', 'openrouter', or 'gemini'
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini").lower()

    # Google Gemini API (for LLM — free tier)
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-flash-latest")
    # Embeddings (OpenRouter NVIDIA Nemotron / Gemini / Local)
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b:free")

    # OpenRouter API (NVIDIA Nemotron 3.5 Lightning / Cloud GPUs)
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3.5-lightning:free")

    # Ollama (Local)
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1")

    # ChromaDB
    CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
    CHROMA_COLLECTION: str = "hospital_knowledge"

    # Database — Render provides postgres:// URLs, SQLAlchemy 2.0 needs postgresql://
    _raw_db_url: str = os.getenv("DATABASE_URL", "sqlite:///./hospital.db")
    DATABASE_URL: str = _raw_db_url.replace("postgres://", "postgresql://", 1) if _raw_db_url.startswith("postgres://") else _raw_db_url

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


settings = Settings()

"""Application configuration loaded from environment variables."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root with override enabled for hot-reloads
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)


class Settings:
    """Central configuration for the healthcare RAG chatbot."""

    # Provider setting: 'groq', 'ollama', 'openrouter', or 'gemini'
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini").lower()

    # Google Gemini API (for LLM and Embeddings — free tier)
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-3.5-flash")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-001")

    # OpenRouter API (Cloud GPUs)
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

    # Groq API (Cloud)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_LLM_MODEL: str = os.getenv("GROQ_LLM_MODEL", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))
    GROQ_LLM_BASE_URL: str = os.getenv("GROQ_LLM_BASE_URL", "https://api.groq.com/openai/v1")

    # Ollama (Local)
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1")

    # ChromaDB
    CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
    CHROMA_COLLECTION: str = "hospital_knowledge"

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./hospital.db")

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
    CACHE_RESPONSE_TTL: int = int(os.getenv("CACHE_RESPONSE_TTL", "120"))     # 2 min


settings = Settings()

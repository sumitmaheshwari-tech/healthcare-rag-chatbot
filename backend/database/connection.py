"""Database connection management."""

import sys
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from .models import Base

# Ensure config is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

_is_sqlite = "sqlite" in settings.DATABASE_URL

def _create_engine_with_fallback():
    global _is_sqlite
    db_url = settings.DATABASE_URL
    if not _is_sqlite:
        try:
            from sqlalchemy.pool import NullPool
            from sqlalchemy import text
            test_engine = create_engine(
                db_url,
                poolclass=NullPool,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 4},
                echo=False,
            )
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("[DB] Connected to PostgreSQL successfully.")
            return test_engine
        except Exception as e:
            print(f"[DB WARNING] Remote PostgreSQL connection failed: {e}. Falling back to SQLite.")
            fallback_sqlite = "sqlite:////tmp/hospital.db" if (os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME")) else "sqlite:///./hospital.db"
            settings.DATABASE_URL = fallback_sqlite
            _is_sqlite = True
            return create_engine(fallback_sqlite, connect_args={"check_same_thread": False}, echo=False)
    else:
        return create_engine(db_url, connect_args={"check_same_thread": False}, echo=False)

engine = _create_engine_with_fallback()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """Return a new database session."""
    return SessionLocal()


def init_db():
    """Create all database tables."""
    try:
        from .models import Base
        Base.metadata.create_all(bind=engine)
        print("[DB] Database tables created successfully.")
    except Exception as e:
        print(f"[DB INIT ERROR] {e}")


# Automatically ensure tables exist on engine creation
try:
    init_db()
except Exception:
    pass

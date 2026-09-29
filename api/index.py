# -*- coding: utf-8 -*-
"""
Vercel Serverless Function entry point for MedCare FastAPI Backend.
"""

import sys
import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
API_DIR = Path(__file__).resolve().parent

for p in [str(ROOT_DIR), str(BACKEND_DIR), str(API_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Also check if backend is inside or sibling of api directory
sibling_backend = API_DIR / "backend"
if sibling_backend.exists() and str(sibling_backend) not in sys.path:
    sys.path.insert(0, str(sibling_backend))

# Ensure Supabase and ChromaDB paths use persistent DB / /tmp on serverless
if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    os.environ["VERCEL"] = "1"
    os.environ.setdefault("ENV", "production")
    
    # Ensure persistent Supabase PostgreSQL connection by default
    SUPABASE_POOLER_URL = "postgresql://postgres.xndvylywqqomrqpknvbo:Sumit%401974@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require"
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or ("sqlite" in db_url and "/tmp" not in db_url):
        os.environ["DATABASE_URL"] = SUPABASE_POOLER_URL
    
    # Fallback secrets if not populated in Vercel Dashboard
    os.environ.setdefault("JWT_SECRET", "medcare-production-jwt-secret-2026")
    os.environ.setdefault("GOOGLE_API_KEY", "AQ.Ab8RN6IIyFKeK8vK9Xh2M7y84iQ0-xZ9")
    os.environ.setdefault("LLM_PROVIDER", "gemini")
    os.environ.setdefault("LLM_MODEL", "gemini-3.5-flash-lite")
    os.environ.setdefault("TELEGRAM_AUTH_BOT_TOKEN", "8630224222:AAFqjxhqGmkuEbZeoeRLrh7M4A30fDD-4uc")
    os.environ.setdefault("TELEGRAM_AUTH_BOT_USERNAME", "MedCare_Verify_Auth_bot")

    chroma_dir = os.environ.get("CHROMA_PERSIST_DIR", "")
    if not chroma_dir or "/tmp" not in chroma_dir:
        os.environ["CHROMA_PERSIST_DIR"] = "/tmp/chroma_db"

try:
    from backend.main import app
except Exception as e:
    import traceback
    startup_error = traceback.format_exc()
    print(f"[FATAL VERCEL STARTUP ERROR]\n{startup_error}")
    
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    app = FastAPI(title="MedCare Startup Diagnostic")
    
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def diagnostic_error_handler(path: str):
        parent_dir = str(API_DIR.parent)
        return PlainTextResponse(
            f"MEDCARE_STARTUP_EXCEPTION:\n{startup_error}\n\n"
            f"sys.path: {sys.path}\n"
            f"cwd: {os.getcwd()}\n"
            f"api dir contents: {os.listdir(str(API_DIR)) if API_DIR.exists() else 'N/A'}\n"
            f"root dir contents: {os.listdir(parent_dir) if os.path.exists(parent_dir) else 'N/A'}\n",
            status_code=500
        )

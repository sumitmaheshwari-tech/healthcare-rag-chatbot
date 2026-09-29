# -*- coding: utf-8 -*-
"""
Vercel Serverless Function entry point for MedCare FastAPI Backend.
"""

import sys
import os
from pathlib import Path

# Add paths to sys.path
current_dir = Path(__file__).resolve().parent
parent_dir = current_dir.parent

for p in [str(current_dir), str(parent_dir), str(parent_dir / "backend"), "/var/task", "/var/task/backend"]:
    if p not in sys.path and os.path.exists(p):
        sys.path.insert(0, p)

# Serverless environment defaults
if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    os.environ["VERCEL"] = "1"
    os.environ.setdefault("ENV", "production")
    
    SUPABASE_POOLER_URL = "postgresql+psycopg2://postgres.wztqumimqknwgeqdvvfc:NTOcGyC5IpSvxqRN@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require"
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or ("sqlite" in db_url and "/tmp" not in db_url):
        os.environ["DATABASE_URL"] = SUPABASE_POOLER_URL
    
    os.environ.setdefault("JWT_SECRET", "medcare-production-jwt-secret-2026")
    os.environ.setdefault("GOOGLE_API_KEY", "AQ.Ab8RN6IIyFKeRnXQGSCxZY49wknN0DpOXqtfDzzTMDJqexHqHg")
    os.environ.setdefault("LLM_PROVIDER", "gemini")
    os.environ.setdefault("LLM_MODEL", "gemini-3.5-flash-lite")
    os.environ.setdefault("TELEGRAM_AUTH_BOT_TOKEN", "8630224222:AAFqjxhqGmkuEbZeoeRLrh7M4A30fDD-4uc")
    os.environ.setdefault("TELEGRAM_AUTH_BOT_USERNAME", "MedCare_Verify_Auth_bot")

    chroma_dir = os.environ.get("CHROMA_PERSIST_DIR", "")
    if not chroma_dir or "/tmp" not in chroma_dir:
        os.environ["CHROMA_PERSIST_DIR"] = "/tmp/chroma_db"

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Initialize top-level app so Vercel always has a valid ASGI entrypoint
app = FastAPI(title="MedCare API Portal")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|.*\.vercel\.app)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_backend_loaded = False
_backend_error = None
_backend_traceback = None

def _load_backend_safely():
    global _backend_loaded, _backend_error, _backend_traceback
    if _backend_loaded:
        return True
    try:
        import backend.main as bm
        # Include all routes from backend.main.app into this app
        for route in bm.app.routes:
            if route not in app.routes:
                app.routes.append(route)
        _backend_loaded = True
        return True
    except Exception as e:
        import traceback
        _backend_error = str(e)
        _backend_traceback = traceback.format_exc()
        print(f"[BACKEND LOAD ERROR]: {_backend_error}\n{_backend_traceback}")
        return False

# Attempt initial load
_load_backend_safely()

@app.get("/api/health")
async def health_check():
    """Health check endpoint that reports system status or backend diagnostics."""
    if _load_backend_safely():
        import backend.main as bm
        # Delegate to backend's health check logic
        return await bm.health_check()
    else:
        return JSONResponse({
            "status": "degraded",
            "backend_loaded": False,
            "error": _backend_error,
            "traceback": _backend_traceback,
            "sys_path": sys.path,
            "cwd": os.getcwd(),
            "cwd_files": os.listdir(".") if os.path.exists(".") else []
        }, status_code=200)

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def api_fallback(path: str, request: Request):
    """Fallback handler in case backend routes could not load."""
    if not _load_backend_safely():
        return JSONResponse({
            "error": "Backend modules failed to initialize",
            "details": _backend_error,
            "traceback": _backend_traceback
        }, status_code=500)
    return JSONResponse({
        "error": f"Path /api/{path} not found",
        "scope_path": request.scope.get("path"),
        "headers": dict(request.headers)
    }, status_code=404)

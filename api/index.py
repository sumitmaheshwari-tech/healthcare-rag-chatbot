# -*- coding: utf-8 -*-
"""
Vercel Serverless Function entry point for MedCare FastAPI Backend.
"""

import sys
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode

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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Attempt to load the primary backend FastAPI application
try:
    import backend.main as bm
    backend_app = bm.app
    _load_error = None
except Exception as e:
    import traceback
    _load_error = {
        "error": "Backend modules failed to initialize",
        "details": str(e),
        "traceback": traceback.format_exc()
    }
    backend_app = FastAPI(title="MedCare API Portal (Degraded)")

    @backend_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def degraded_fallback(path: str):
        return JSONResponse(_load_error, status_code=500)


class VercelASGIApp:
    """
    Transparent ASGI wrapper that restores the original request path from
    the Vercel rewrite parameter '__orig_path', strips it from the query string,
    and forwards the call cleanly to FastAPI.
    """
    def __init__(self, target_app):
        self.target_app = target_app
        self.routes = getattr(target_app, "routes", [])
        self.router = getattr(target_app, "router", None)
        self.middleware_stack = getattr(target_app, "middleware_stack", None)

    def __getattr__(self, name):
        return getattr(self.target_app, name)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
            params = parse_qs(query_string)

            if "__orig_path" in params and params["__orig_path"]:
                orig_path = params.pop("__orig_path")[0]
                scope["path"] = orig_path
                scope["raw_path"] = orig_path.encode("utf-8")
                # Re-encode query string without the __orig_path parameter
                scope["query_string"] = urlencode(params, doseq=True).encode("utf-8")
            elif scope.get("path") in ["/api/index.py", "/api"]:
                scope["path"] = "/api/health"
                scope["raw_path"] = b"/api/health"

        await self.target_app(scope, receive, send)


app = VercelASGIApp(backend_app)

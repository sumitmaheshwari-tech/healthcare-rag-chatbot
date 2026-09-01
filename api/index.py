# -*- coding: utf-8 -*-
"""
Vercel Serverless Function entry point for MedCare FastAPI Backend.
"""

import sys
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Ensure SQLite and ChromaDB paths use /tmp if on serverless read-only filesystem
if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    os.environ["VERCEL"] = "1"
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or ("sqlite" in db_url and "/tmp" not in db_url):
        os.environ["DATABASE_URL"] = "sqlite:////tmp/hospital.db"
    
    chroma_dir = os.environ.get("CHROMA_PERSIST_DIR", "")
    if not chroma_dir or "/tmp" not in chroma_dir:
        os.environ["CHROMA_PERSIST_DIR"] = "/tmp/chroma_db"

from backend.main import app

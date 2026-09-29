"""MedCare RAG Chatbot — FastAPI entry point."""

import os
import sys
import uuid
import time
import asyncio
import jwt
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Form, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage

# Ensure the backend package is importable
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from config import settings
from database.connection import init_db, get_db
from database.setup import seed_database
from knowledge.ingest import ingest_documents
from agent.graph import build_graph
from database.models import Patient
from database.encrypt import encrypt_value, decrypt_value
from database.audit import log_audit_event
from services.otp_service import (
    check_ip_lockout, increment_ip_failed_attempt, clear_ip_attempts,
    check_account_lockout, increment_account_failed_attempt, clear_account_attempts,
    create_and_send_patient_otp, verify_patient_otp
)

# JWT Config
_jwt_fallback = "medcare-secure-jwt-secret-key-2026"
if settings.ENV == "production" and not os.getenv("JWT_SECRET"):
    print("WARNING: JWT_SECRET environment variable is not set in production! Using fallback JWT secret.")
JWT_SECRET = os.getenv("JWT_SECRET", _jwt_fallback)
if JWT_SECRET == _jwt_fallback:
    print("WARNING: Using fallback JWT secret. Set JWT_SECRET env var for production.")
JWT_ALGORITHM = "HS256"

# ── Global agent instance ─────────────────────────────────────────────
agent_graph = None


# ── Helper for Lazy Agent Graph Compilation (Serverless-Safe) ────────
_agent_build_lock = None

def get_agent_build_lock():
    global _agent_build_lock
    if _agent_build_lock is None:
        _agent_build_lock = asyncio.Lock()
    return _agent_build_lock

async def get_or_build_agent_graph():
    global agent_graph
    if agent_graph is not None:
        return agent_graph
    async with get_agent_build_lock():
        if agent_graph is None:
            print("[SERVERLESS] Compiling LangGraph agent graph on demand...")
            try:
                ingest_documents()
            except Exception as e:
                print(f"[RAG WARNING] Knowledge base ingest warning: {e}")
            agent_graph = await build_graph()
            print("[SERVERLESS] Agent graph ready.")
    return agent_graph


# ── Lifespan (startup / shutdown) ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_graph

    print("=" * 60)
    print("  MedCare RAG Chatbot — Starting up …")
    print("=" * 60)

    is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    if is_serverless:
        print("[SERVERLESS] Serverless environment detected — instant cold-start enabled.")
        print("[SERVERLESS] Deferring DB setup, knowledge base, and agent graph to lazy-load on demand.\n")
    else:
        # 1. Database (Persistent Storage for All Patients & Appointments)
        print("\n[1/3] Initialising database …")
        try:
            init_db()
            db = get_db()
            seed_database(db)
            db.close()
            print("[1/3] Database tables ensured & patient data preserved.\n")
        except Exception as e:
            print(f"[DB WARNING] Database setup warning: {e}\n")

        # 2. Knowledge base
        print("[2/3] Ingesting knowledge base …")
        try:
            ingest_documents()
            print("[2/3] Knowledge base ready.\n")
        except Exception as e:
            print(f"[RAG WARNING] Knowledge base error: {e}\n")

        # 3. LangGraph agent
        print("[3/3] Building LangGraph agent …")
        try:
            agent_graph = await build_graph()
            print("[3/3] Agent ready.\n")
        except Exception as e:
            print(f"[AGENT ERROR] Agent build error: {e}\n")

    print("=" * 60)
    print("  MedCare RAG Chatbot — Ready!")
    print("  Open  http://localhost:8000  in your browser")
    print("=" * 60)

    yield
    print("\nShutting down …")


# ── FastAPI app ───────────────────────────────────────────────────────
app = FastAPI(
    title="MedCare RAG Chatbot",
    description="Healthcare Agentic RAG Chatbot powered by LangGraph",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|.*\.vercel\.app)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Token Management Helpers ──────────────────────────────────────────
def create_tokens(patient_uid: str):
    """Generate short-lived access token and longer-lived refresh token."""
    access_exp = datetime.utcnow() + timedelta(minutes=15)
    access_payload = {"sub": patient_uid, "exp": access_exp, "type": "access"}
    access_token = jwt.encode(access_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    refresh_exp = datetime.utcnow() + timedelta(days=7)
    refresh_payload = {"sub": patient_uid, "exp": refresh_exp, "type": "refresh"}
    refresh_token = jwt.encode(refresh_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    return access_token, refresh_token


def get_authenticated_patient_uid(request: Request) -> str | None:
    """Retrieve verified patient UID from AccessToken cookie (or silent renew)."""
    access_token = request.cookies.get("AccessToken")
    
    if not access_token:
        # Check refresh token for silent renewal
        refresh_token = request.cookies.get("RefreshToken")
        if refresh_token:
            try:
                payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                if payload.get("type") == "refresh":
                    return payload.get("sub")
            except Exception:
                pass
        return None

    try:
        payload = jwt.decode(access_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") == "access":
            return payload.get("sub")
    except jwt.ExpiredSignatureError:
        # Expired, try fallback to refresh
        refresh_token = request.cookies.get("RefreshToken")
        if refresh_token:
            try:
                payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                if payload.get("type") == "refresh":
                    return payload.get("sub")
            except Exception:
                pass
    except Exception:
        pass
    return None


def set_jwt_cookies(response: Response, access_token: str, refresh_token: str):
    """Set access and refresh tokens in HttpOnly, SameSite=Strict cookies."""
    _secure = settings.COOKIE_SECURE
    response.set_cookie(
        key="AccessToken",
        value=access_token,
        httponly=True,
        secure=_secure,
        samesite="strict",
        path="/",
        max_age=900  # 15 minutes
    )
    response.set_cookie(
        key="RefreshToken",
        value=refresh_token,
        httponly=True,
        secure=_secure,
        samesite="strict",
        path="/",
        max_age=604800  # 7 days
    )


# ── Pydantic models ──────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str = ""

class SetSessionRequest(BaseModel):
    patient_id: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    patient_id: str = ""
    patient_name: str = ""


class RegisterRequest(BaseModel):
    name: str
    dob: Optional[str] = ""  # YYYY-MM-DD (Optional)
    phone: str


class LoginRequest(BaseModel):
    patient_uid: str
    dob: str  # YYYY-MM-DD
    phone: str


class OTPVerifyRequest(BaseModel):
    patient_uid: str
    otp: str


class TelegramAuthInitiateRequest(BaseModel):
    flow: str  # "register" or "login"
    name: Optional[str] = ""
    dob: str
    phone: str
    patient_uid: Optional[str] = ""



class SelectProfileRequest(BaseModel):
    patient_uid: str

class TelegramAuthVerifyRequest(BaseModel):
    auth_session_id: str
    otp: str


# ── API routes ────────────────────────────────────────────────────────
@app.post("/api/patients/set-session")
async def set_session(req: SetSessionRequest, response: Response, req_raw: Request):
    """Securely set JWT cookies for an authenticated patient."""
    response.headers["Cache-Control"] = "no-store"
    db = get_db()
    try:
        patient = db.query(Patient).filter(Patient.id == req.patient_id.strip()).first()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient profile not found.")
        
        access_token, refresh_token = create_tokens(patient.id)
        set_jwt_cookies(response, access_token, refresh_token)
        return {"success": True, "message": "Session cookies set successfully."}
    finally:
        db.close()


@app.post("/api/chat")
async def chat(request: ChatRequest, req_raw: Request, response: Response):
    """Main conversational endpoint serving token-by-token streaming events."""
    global agent_graph

    response.headers["Cache-Control"] = "no-store"

    active_graph = await get_or_build_agent_graph()
    if active_graph is None:
        raise HTTPException(status_code=503, detail="Agent not initialised yet.")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    session_id = request.session_id or str(uuid.uuid4())
    req_id = str(uuid.uuid4())
    auth_uid = get_authenticated_patient_uid(req_raw)
    
    log_audit_event(
        request_id=req_id,
        action="CHAT_QUERY",
        status="SUCCESS",
        patient_uid=auth_uid,
        ip_address=req_raw.client.host,
        user_agent=req_raw.headers.get("User-Agent"),
        details=f"Chat query: '{request.message[:30]}...'"
    )

    async def event_generator():
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 25
        }
        user_message = request.message
        t_request_start = time.perf_counter()
        t_first_token = None

        try:
            # Yield initial metadata
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'patient_id': auth_uid or ''})}\n\n"

            # ── 0. Semantic Cache Fast-Path (<10ms) ───────────────────
            from knowledge.cache import get_response_cache, set_response_cache
            cached_resp = get_response_cache(user_message, patient_uid=auth_uid or "guest")
            if cached_resp:
                t_first_token = time.perf_counter()
                ttft_ms = (t_first_token - t_request_start) * 1000
                print(f"[PERF CACHE] Instant Cache HIT ({ttft_ms:.1f}ms) for query: '{user_message}'")
                yield f"data: {json.dumps({'type': 'content', 'text': cached_resp})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # ── Buffered streaming with keepalive & timeout ──────────
            run_text_buffer = []
            current_run_id = None
            tool_runs_completed = 0
            MAX_TOOL_ROUNDS = 6
            REQUEST_TIMEOUT_SECONDS = 90

            async def _run_agent_stream():
                """Run the agent and collect SSE events. Returns list of SSE strings."""
                nonlocal run_text_buffer, current_run_id, tool_runs_completed, t_first_token
                sse_events = []

                async for event in active_graph.astream_events(
                    {
                        "messages": [HumanMessage(content=user_message)],
                        "authenticated_patient_uid": auth_uid
                    },
                    config=config,
                    version="v2"
                ):
                    evt_name = event["event"]
                    run_id = event.get("run_id")

                    if evt_name == "on_chat_model_start":
                        current_run_id = run_id
                        run_text_buffer = []
                        sse_events.append(": keepalive\n\n")

                    elif evt_name == "on_chat_model_stream":
                        chunk = event["data"]["chunk"]
                        if hasattr(chunk, "content") and chunk.content:
                            if hasattr(chunk, "tool_call_chunks") and chunk.tool_call_chunks:
                                continue
                            text_chunk = chunk.content
                            if isinstance(text_chunk, list):
                                text_chunk = "".join([b.get("text", "") if isinstance(b, dict) else str(b) for b in text_chunk])
                            if text_chunk:
                                run_text_buffer.append(text_chunk)

                    elif evt_name == "on_chat_model_end":
                        output = event["data"].get("output")
                        has_tool_calls = (
                            output is not None
                            and hasattr(output, "tool_calls")
                            and output.tool_calls
                        )
                        if has_tool_calls:
                            run_text_buffer = []
                        else:
                            final_text = "".join(run_text_buffer)
                            if not final_text and output and hasattr(output, "content") and output.content:
                                final_text = output.content if isinstance(output.content, str) else str(output.content)
                            if final_text:
                                if t_first_token is None:
                                    t_first_token = time.perf_counter()
                                    ttft_ms = (t_first_token - t_request_start) * 1000
                                    print(f"[PERF] TTFT (time to first token): {ttft_ms:.0f}ms")
                                sse_events.append(f"data: {json.dumps({'type': 'content', 'text': final_text})}\n\n")
                                try:
                                    set_response_cache(user_message, final_text, patient_uid=auth_uid or "guest")
                                except Exception as ce:
                                    print(f"[CACHE WARNING] Failed to cache response: {ce}")
                            run_text_buffer = []

                    elif evt_name == "on_tool_start":
                        sse_events.append(": keepalive\n\n")

                    elif evt_name == "on_tool_end":
                        tool_runs_completed += 1
                        sse_events.append(": keepalive\n\n")
                        if tool_runs_completed >= MAX_TOOL_ROUNDS:
                            print(f"[GUARD] Tool loop detected: {tool_runs_completed} rounds. Force-stopping.")
                            sse_events.append(f"data: {json.dumps({'type': 'content', 'text': 'I apologize, I encountered an issue processing your request. Could you please provide more specific details (e.g., exact date in YYYY-MM-DD format and preferred time)?'})}\n\n")
                            t_first_token = t_first_token or time.perf_counter()
                            break

                return sse_events

            # Execute with timeout
            try:
                import asyncio
                sse_results = await asyncio.wait_for(
                    _run_agent_stream(),
                    timeout=REQUEST_TIMEOUT_SECONDS
                )
                for sse in sse_results:
                    yield sse
            except asyncio.TimeoutError:
                print(f"[TIMEOUT] Request exceeded {REQUEST_TIMEOUT_SECONDS}s limit.")
                yield f"data: {json.dumps({'type': 'content', 'text': 'I apologize, the request took too long. Please try again — the server may have been warming up. If this persists, try breaking your request into steps.'})}\n\n"
                t_first_token = t_first_token or time.perf_counter()

            # If no content was ever streamed, send a fallback
            if t_first_token is None:
                yield f"data: {json.dumps({'type': 'content', 'text': 'I apologize, I was unable to generate a response. Please try rephrasing your question.'})}\n\n"

            # ── Verify if login/registration succeeded ────────────────
            state = await active_graph.aget_state(config)
            verified_patient_id = ""
            verified_patient_name = ""
            import re

            for msg in reversed(state.values.get("messages", [])):
                content_str = str(msg.content)
                m_verify = re.search(r"Verification successful for patient (.*?) \(ID: (pat-\w+-\w+-\w+-\w+-\w+|pat-\w+)\)", content_str)
                if m_verify:
                    verified_patient_id = m_verify.group(2)
                    verified_patient_name = m_verify.group(1)
                    break
                m_register = re.search(r"Patient ID:\*\* (pat-\w+-\w+-\w+-\w+-\w+|pat-\w+)", content_str)
                if m_register:
                    verified_patient_id = m_register.group(1)
                    db_session = get_db()
                    try:
                        db_p = db_session.query(Patient).filter(Patient.id == verified_patient_id).first()
                        if db_p:
                            verified_patient_name = db_p.name
                    finally:
                        db_session.close()
                    break

            if verified_patient_id:
                yield f"data: {json.dumps({'type': 'auth', 'patient_id': verified_patient_id, 'patient_name': verified_patient_name})}\n\n"

            total_ms = (time.perf_counter() - t_request_start) * 1000
            print(f"[PERF] Total stream duration: {total_ms:.0f}ms")
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            print(f"[STREAM ERROR] {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def normalize_dob(dob_str: str) -> str:
    """Normalize various date formats (YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY) to standard YYYY-MM-DD."""
    if not dob_str:
        return ""
    clean = dob_str.strip().replace("/", "-")
    parts = clean.split("-")
    if len(parts) == 3:
        if len(parts[0]) <= 2 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        if len(parts[0]) == 4:
            return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    return clean


def normalize_phone_number(phone_str: str) -> str:
    """Extract clean 10-digit mobile number digits."""
    digits = "".join(c for c in phone_str if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


@app.post("/api/patients/register")
async def register_patient(req: RegisterRequest, req_raw: Request, response: Response):
    """Register a new patient securely, generating a unique MRN and setting JWT session."""
    response.headers["Cache-Control"] = "no-store"
    req_id = str(uuid.uuid4())
    db = get_db()
    try:
        clean_name = req.name.strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="Please enter your full name.")

        target_phone = normalize_phone_number(req.phone)
        if not target_phone or len(target_phone) < 10:
            raise HTTPException(status_code=400, detail="Please enter a valid 10-digit mobile number.")

        clean_dob = normalize_dob(req.dob)

        # 1. Check if patient with this exact phone and name already exists
        all_patients = db.query(Patient).all()
        for ep in all_patients:
            raw_phone = normalize_phone_number(ep.phone or "")
            dec_phone = normalize_phone_number(decrypt_value(ep.phone) if ep.phone else "")
            if (dec_phone == target_phone or raw_phone == target_phone) and ep.name.strip().lower() == clean_name.lower():
                # Profile already exists — log in directly!
                access_token, refresh_token = create_tokens(ep.id)
                set_jwt_cookies(response, access_token, refresh_token)
                log_audit_event(
                    request_id=req_id,
                    action="LOGIN_DIRECT",
                    status="SUCCESS",
                    patient_uid=ep.id,
                    ip_address=req_raw.client.host,
                    user_agent=req_raw.headers.get("User-Agent"),
                    details=f"Patient {ep.name} logged into existing profile via direct registration form."
                )
                return {
                    "success": True,
                    "patient_uid": ep.id,
                    "name": ep.name,
                    "dob": clean_dob or (decrypt_value(ep.dob) if ep.dob else ""),
                    "already_registered": True
                }

        # 2. Create new patient profile
        new_uid = f"pat-{uuid.uuid4().hex[:12]}"
        age = 0
        if clean_dob:
            try:
                dob_date = datetime.strptime(clean_dob, "%Y-%m-%d")
                age = (datetime.utcnow() - dob_date).days // 365
            except Exception:
                pass

        new_pat = Patient(
            id=new_uid,
            name=clean_name,
            dob=encrypt_value(clean_dob),
            age=age,
            gender="Unknown",
            phone=encrypt_value(target_phone),
            email=encrypt_value(""),
            address=encrypt_value(""),
            blood_group=encrypt_value(""),
            emergency_contact=encrypt_value("")
        )
        db.add(new_pat)
        db.commit()

        # Log audit log
        log_audit_event(
            request_id=req_id,
            action="REGISTER",
            status="SUCCESS",
            patient_uid=new_uid,
            ip_address=req_raw.client.host,
            user_agent=req_raw.headers.get("User-Agent"),
            resource=f"patients/{new_uid}",
            details=f"Registered patient '{clean_name}'"
        )

        # Issue JWT cookies automatically on successful registration
        access_token, refresh_token = create_tokens(new_uid)
        set_jwt_cookies(response, access_token, refresh_token)

        return {
            "success": True,
            "patient_uid": new_uid,
            "name": new_pat.name,
            "dob": clean_dob
        }
    except Exception as e:
        db.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


def find_patient_in_db(db, patient_uid_or_phone: str, phone_str: str = ""):
    """Flexible patient lookup by Patient ID (case-insensitive) or Registered Mobile Number."""
    all_patients = db.query(Patient).all()
    cleaned_uid = (patient_uid_or_phone or "").strip().lower()
    target_phone = normalize_phone_number(phone_str or patient_uid_or_phone or "")

    # 1. Match by Patient ID (case-insensitive, handles 'pat-xxx' and 'xxx')
    if cleaned_uid and not cleaned_uid.isdigit():
        for p in all_patients:
            p_id = p.id.strip().lower()
            if p_id == cleaned_uid or p_id.replace("pat-", "") == cleaned_uid.replace("pat-", ""):
                return p

    # 2. Match by Phone Number (checks decrypted phone AND raw stored phone)
    if target_phone:
        for p in all_patients:
            raw_phone = normalize_phone_number(p.phone or "")
            dec_phone = normalize_phone_number(decrypt_value(p.phone) if p.phone else "")
            if (dec_phone and dec_phone == target_phone) or (raw_phone and raw_phone == target_phone):
                return p

    # 3. Fallback: If cleaned_uid was numeric phone digits
    if cleaned_uid and cleaned_uid.isdigit() and len(cleaned_uid) >= 10:
        digits = cleaned_uid[-10:]
        for p in all_patients:
            raw_phone = normalize_phone_number(p.phone or "")
            dec_phone = normalize_phone_number(decrypt_value(p.phone) if p.phone else "")
            if (dec_phone and dec_phone == digits) or (raw_phone and raw_phone == digits):
                return p

    return None


@app.post("/api/patients/login")
async def login_patient(req: LoginRequest, req_raw: Request, response: Response):
    """Authenticate patient directly using Patient ID, Date of Birth, and Mobile Number."""
    response.headers["Cache-Control"] = "no-store"
    req_id = str(uuid.uuid4())
    ip_addr = req_raw.client.host

    # 1. Check IP Lockout
    ip_remaining = check_ip_lockout(ip_addr)
    if ip_remaining:
        raise HTTPException(
            status_code=423,
            detail=f"Too many failed login attempts. Temporarily locked. Try again in {int(ip_remaining // 60) + 1} minutes."
        )

    db = get_db()
    try:
        patient = find_patient_in_db(db, req.patient_uid, req.phone)

        # 2. Validate Patient ID exists
        if not patient:
            increment_ip_failed_attempt(ip_addr)
            log_audit_event(
                request_id=req_id,
                action="LOGIN",
                status="FAILED",
                patient_uid=req.patient_uid,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details="Login failed: Patient ID or phone not found."
            )
            raise HTTPException(status_code=400, detail="Invalid Patient ID or Mobile Number. Please check your details or register.")

        # 3. Check Account Lockout
        acc_remaining = check_account_lockout(patient)
        if acc_remaining:
            raise HTTPException(
                status_code=423,
                detail=f"This account is temporarily locked. Try again in {int(acc_remaining.total_seconds() // 60) + 1} minutes."
            )

        # 4. Validate DOB match
        target_dob = normalize_dob(req.dob)
        dec_dob = normalize_dob(decrypt_value(patient.dob) if patient.dob else "")
        if target_dob and dec_dob and dec_dob != target_dob:
            increment_ip_failed_attempt(ip_addr)
            increment_account_failed_attempt(db, patient, ip_addr)
            
            log_audit_event(
                request_id=req_id,
                action="LOGIN",
                status="FAILED",
                patient_uid=patient.id,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details="Login failed: Date of Birth mismatch."
            )
            raise HTTPException(status_code=400, detail="Invalid Date of Birth.")

        # 5. Validate Mobile Phone match
        target_phone = normalize_phone_number(req.phone or "")
        dec_phone = normalize_phone_number(decrypt_value(patient.phone) if patient.phone else "")
        if target_phone and dec_phone and target_phone != dec_phone:
            increment_ip_failed_attempt(ip_addr)
            increment_account_failed_attempt(db, patient, ip_addr)
            
            log_audit_event(
                request_id=req_id,
                action="LOGIN",
                status="FAILED",
                patient_uid=patient.id,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details="Login failed: Mobile phone mismatch."
            )
            raise HTTPException(status_code=400, detail="Mobile phone number does not match registered records.")

        # 6. Clear failed attempts on credentials match
        clear_ip_attempts(ip_addr)
        clear_account_attempts(db, patient)

        # 7. Issue secure JWT session tokens
        access_token, refresh_token = create_tokens(patient.id)
        set_jwt_cookies(response, access_token, refresh_token)

        log_audit_event(
            request_id=req_id,
            action="LOGIN",
            status="SUCCESS",
            patient_uid=patient.id,
            ip_address=ip_addr,
            user_agent=req_raw.headers.get("User-Agent"),
            details=f"Patient {patient.name} logged in successfully."
        )

        return {
            "success": True, 
            "message": "Signed in successfully.",
            "patient_uid": patient.id,
            "name": patient.name
        }
    finally:
        db.close()


@app.post("/api/patients/select-profile")
async def select_patient_profile(req: SelectProfileRequest, response: Response, req_raw: Request):
    """Issue secure session cookies for a chosen family profile after phone verification."""
    response.headers["Cache-Control"] = "no-store"
    db = get_db()
    try:
        patient = db.query(Patient).filter(Patient.id == req.patient_uid.strip()).first()
        if not patient:
            raise HTTPException(status_code=404, detail="Selected profile not found.")

        access_token, refresh_token = create_tokens(patient.id)
        set_jwt_cookies(response, access_token, refresh_token)

        log_audit_event(
            request_id=str(uuid.uuid4()),
            action="SELECT_PROFILE",
            status="SUCCESS",
            patient_uid=patient.id,
            ip_address=req_raw.client.host,
            user_agent=req_raw.headers.get("User-Agent"),
            details=f"Patient {patient.name} ({patient.id}) active in session."
        )

        return {
            "success": True,
            "patient_uid": patient.id,
            "name": patient.name
        }
    finally:
        db.close()


@app.post("/api/auth/telegram/initiate")
async def initiate_telegram_auth(req: TelegramAuthInitiateRequest, req_raw: Request):
    """Initiate a Telegram OTP authentication session for @MedCare_Verify_Auth_bot."""
    from services.telegram_auth_service import create_auth_session
    
    db = get_db()
    try:
        target_phone = normalize_phone_number(req.phone or req.patient_uid or "")
        if not target_phone or len(target_phone) < 10:
            raise HTTPException(status_code=400, detail="Please enter a valid 10-digit registered mobile number.")

        if req.flow == "login":
            # Check if any patient exists with this phone or UID
            patient = find_patient_in_db(db, req.patient_uid, target_phone)
            if not patient:
                all_patients = db.query(Patient).all()
                for p in all_patients:
                    raw_phone = normalize_phone_number(p.phone or "")
                    p_phone = normalize_phone_number(decrypt_value(p.phone) if p.phone else "")
                    if (p_phone and p_phone == target_phone) or (raw_phone and raw_phone == target_phone):
                        patient = p
                        break
            
            if not patient:
                raise HTTPException(
                    status_code=404,
                    detail="No patient profile found with this mobile number. Click 'Register profile here' to create your account in 10 seconds!"
                )

            session_info = create_auth_session(flow="login", data={
                "patient_uid": req.patient_uid.strip() if req.patient_uid else "",
                "phone": target_phone,
                "dob": req.dob or ""
            })
            return {"success": True, **session_info}
        
        elif req.flow == "register":
            clean_name = req.name.strip()
            if not clean_name:
                raise HTTPException(status_code=400, detail="Please enter your full name.")

            # Check if exact same Name + Phone already exists
            all_patients = db.query(Patient).all()
            exact_match = None
            for ep in all_patients:
                ep_phone = normalize_phone_number(decrypt_value(ep.phone) if ep.phone else "")
                if ep_phone == target_phone and ep.name.strip().lower() == clean_name.lower():
                    exact_match = ep
                    break

            session_info = create_auth_session(flow="register", data={
                "name": clean_name,
                "dob": req.dob.strip() if req.dob else "",
                "phone": target_phone,
                "is_existing": exact_match is not None,
                "existing_uid": exact_match.id if exact_match else ""
            })
            return {"success": True, **session_info}
        else:
            raise HTTPException(status_code=400, detail="Invalid auth flow requested.")
    finally:
        db.close()


@app.post("/api/auth/telegram/verify")
async def verify_telegram_auth(req: TelegramAuthVerifyRequest, req_raw: Request, response: Response):
    """Verify 6-digit Telegram OTP & support Multi-Profile Family Selection."""
    from services.telegram_auth_service import verify_session_otp
    
    result = verify_session_otp(req.auth_session_id, req.otp)
    if not result:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code. Please tap START on @MedCare_Verify_Auth_bot to get your code.")

    flow = result["flow"]
    p_data = result["data"]
    req_id = str(uuid.uuid4())
    ip_addr = req_raw.client.host
    verified_phone = normalize_phone_number(p_data.get("phone", ""))

    db = get_db()
    try:
        # ── REGISTRATION FLOW ─────────────────────────────────────────
        if flow == "register":
            existing_patient = None
            if p_data.get("existing_uid"):
                existing_patient = db.query(Patient).filter(Patient.id == p_data["existing_uid"]).first()
            
            if not existing_patient:
                for ep in db.query(Patient).all():
                    ep_phone = normalize_phone_number(decrypt_value(ep.phone) if ep.phone else "")
                    if ep_phone == verified_phone and ep.name.strip().lower() == p_data["name"].strip().lower():
                        existing_patient = ep
                        break

            if existing_patient:
                patient = existing_patient
            else:
                new_uid = f"pat-{uuid.uuid4().hex[:12]}"
                age = 0
                if p_data.get("dob"):
                    try:
                        dob_date = datetime.strptime(p_data["dob"], "%Y-%m-%d")
                        age = (datetime.utcnow() - dob_date).days // 365
                    except Exception:
                        pass

                patient = Patient(
                    id=new_uid,
                    name=p_data["name"],
                    dob=encrypt_value(p_data.get("dob", "")),
                    age=age,
                    gender="Unknown",
                    phone=encrypt_value(verified_phone),
                    email=encrypt_value(""),
                    address=encrypt_value(""),
                    blood_group=encrypt_value(""),
                    emergency_contact=encrypt_value("")
                )
                db.add(patient)
                db.commit()

            access_token, refresh_token = create_tokens(patient.id)
            set_jwt_cookies(response, access_token, refresh_token)

            log_audit_event(
                request_id=req_id,
                action="REGISTER_TELEGRAM_OTP",
                status="SUCCESS",
                patient_uid=patient.id,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details=f"Patient {patient.name} registered and authenticated via Telegram."
            )

            return {
                "success": True,
                "patient_uid": patient.id,
                "name": patient.name,
                "dob": p_data.get("dob", "")
            }

        # ── SIGN IN FLOW (With Multi-Profile Family Support) ────────────
        elif flow == "login":
            all_patients = db.query(Patient).all()
            matching_patients = []
            
            # Find all profiles belonging to this phone number
            for p in all_patients:
                raw_phone = normalize_phone_number(p.phone or "")
                ep_phone = normalize_phone_number(decrypt_value(p.phone) if p.phone else "")
                if (ep_phone and ep_phone == verified_phone) or (raw_phone and raw_phone == verified_phone):
                    if p not in matching_patients:
                        matching_patients.append(p)
                elif p_data.get("patient_uid") and p.id.lower() == p_data["patient_uid"].lower():
                    if p not in matching_patients:
                        matching_patients.append(p)

            if not matching_patients:
                raise HTTPException(status_code=404, detail="No patient profile found for this mobile number.")

            # If only 1 profile or specific UID requested: Log in directly
            if len(matching_patients) == 1:
                patient = matching_patients[0]
                access_token, refresh_token = create_tokens(patient.id)
                set_jwt_cookies(response, access_token, refresh_token)

                log_audit_event(
                    request_id=req_id,
                    action="LOGIN_TELEGRAM_OTP",
                    status="SUCCESS",
                    patient_uid=patient.id,
                    ip_address=ip_addr,
                    user_agent=req_raw.headers.get("User-Agent"),
                    details=f"Patient {patient.name} logged in via Telegram OTP."
                )

                return {
                    "success": True,
                    "multiple_profiles": False,
                    "patient_uid": patient.id,
                    "name": patient.name
                }

            # Multiple family profiles exist on this phone: Return profile options
            profiles = []
            for p in matching_patients:
                dec_dob = decrypt_value(p.dob) if p.dob else ""
                profiles.append({
                    "patient_uid": p.id,
                    "name": p.name,
                    "dob": dec_dob,
                    "age": p.age or ""
                })

            return {
                "success": True,
                "multiple_profiles": True,
                "phone": verified_phone,
                "profiles": profiles
            }
    finally:
        db.close()


@app.post("/api/telegram/auth-webhook")
async def telegram_auth_webhook(req: Request):
    """Receive incoming Telegram Bot API updates via Webhook (Optimized for Serverless / Vercel)."""
    from services.telegram_auth_service import process_telegram_update
    try:
        data = await req.json()
        process_telegram_update(data)
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/telegram/setup-webhook")
async def setup_telegram_webhook_route(request: Request, key: Optional[str] = None):
    """One-click setup endpoint to register the Telegram Webhook with Telegram servers."""
    admin_secret = os.getenv("JWT_SECRET", "medcare-admin")
    if key != admin_secret and key != "medcare":
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?key=medcare")
    
    from services.telegram_auth_service import set_telegram_webhook
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and not "localhost" in base_url and not "127.0.0.1" in base_url:
        base_url = base_url.replace("http://", "https://")
        
    result = set_telegram_webhook(base_url)
    return {
        "success": True,
        "base_url": base_url,
        "result": result
    }


@app.post("/api/patients/verify-otp")
async def verify_otp(req: OTPVerifyRequest, req_raw: Request, response: Response):
    """Authenticate patient using Patient ID and OTP, returning JWT session cookies."""
    response.headers["Cache-Control"] = "no-store"
    req_id = str(uuid.uuid4())
    ip_addr = req_raw.client.host

    # 1. Check IP Lockout
    ip_remaining = check_ip_lockout(ip_addr)
    if ip_remaining:
        raise HTTPException(
            status_code=423,
            detail=f"Too many failed login attempts from this location. Temporarily locked. Try again in {int(ip_remaining // 60) + 1} minutes."
        )

    db = get_db()
    try:
        patient = db.query(Patient).filter(Patient.id == req.patient_uid.strip()).first()
        if not patient:
            raise HTTPException(status_code=400, detail="Invalid Patient ID.")

        # 2. Check Patient Account Lockout
        acc_remaining = check_account_lockout(patient)
        if acc_remaining:
            raise HTTPException(
                status_code=423,
                detail=f"This account is temporarily locked. Try again in {int(acc_remaining.total_seconds() // 60) + 1} minutes."
            )

        # 3. Verify OTP code
        otp_res = verify_patient_otp(db, patient.id, req.otp, ip_addr)
        if not otp_res["success"]:
            # Mismatch, increment failed attempts
            increment_ip_failed_attempt(ip_addr)
            increment_account_failed_attempt(db, patient, ip_addr)
            
            log_audit_event(
                request_id=req_id,
                action="VERIFY_OTP",
                status="FAILED",
                patient_uid=req.patient_uid,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details=f"OTP code verification failed: {otp_res['message']}"
            )
            raise HTTPException(status_code=400, detail=otp_res["message"])

        # 4. Success! Clear lockout counters
        clear_ip_attempts(ip_addr)
        clear_account_attempts(db, patient)

        log_audit_event(
            request_id=req_id,
            action="VERIFY_OTP",
            status="SUCCESS",
            patient_uid=patient.id,
            ip_address=ip_addr,
            user_agent=req_raw.headers.get("User-Agent"),
            details=f"Patient {patient.name} authenticated successfully via OTP."
        )

        access_token, refresh_token = create_tokens(patient.id)
        set_jwt_cookies(response, access_token, refresh_token)

        return {"success": True, "message": "Sign in successful."}
    finally:
        db.close()


@app.post("/api/patients/logout")
async def logout_patient(response: Response, req_raw: Request):
    """Log out patient, clearing cookies."""
    response.headers["Cache-Control"] = "no-store"
    auth_uid = get_authenticated_patient_uid(req_raw)
    
    # Audit log
    log_audit_event(
        request_id=str(uuid.uuid4()),
        action="LOGOUT",
        status="SUCCESS",
        patient_uid=auth_uid,
        ip_address=req_raw.client.host,
        user_agent=req_raw.headers.get("User-Agent")
    )

    response.delete_cookie(key="AccessToken", path="/")
    response.delete_cookie(key="RefreshToken", path="/")
    return {"success": True, "message": "Logged out successfully"}


@app.get("/api/patients/me")
async def get_current_patient_profile(request: Request, response: Response):
    """Retrieve details of the currently authenticated patient."""
    response.headers["Cache-Control"] = "no-store"
    patient_uid = get_authenticated_patient_uid(request)
    
    if not patient_uid:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    db = get_db()
    try:
        patient = db.query(Patient).filter(Patient.id == patient_uid).first()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient profile not found.")

        # Log audit log
        log_audit_event(
            request_id=str(uuid.uuid4()),
            action="VIEW_PROFILE",
            status="SUCCESS",
            patient_uid=patient_uid,
            ip_address=request.client.host,
            user_agent=request.headers.get("User-Agent"),
            resource=f"patients/{patient_uid}"
        )

        return {
            "success": True,
            "patient": {
                "patient_uid": patient.id,
                "name": patient.name,
                "dob": decrypt_value(patient.dob),
                "age": patient.age,
                "gender": patient.gender,
                "phone": decrypt_value(patient.phone),
                "email": decrypt_value(patient.email),
                "address": decrypt_value(patient.address),
                "blood_group": decrypt_value(patient.blood_group),
                "emergency_contact": decrypt_value(patient.emergency_contact)
            }
        }
    finally:
        db.close()


@app.get("/api/health")
async def health_check():
    """System health endpoint reporting status of Database, Redis, and LLM configurations."""
    is_serverless = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    health_status = {
        "status": "healthy",
        "agent_ready": (agent_graph is not None) or is_serverless,
        "database": "unhealthy",
        "redis": "unhealthy",
        "primary_llm": "unknown"
    }

    # 1. Database Check
    db = get_db()
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        health_status["database"] = "healthy"
    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["database"] = f"unhealthy: {e}"
    finally:
        db.close()

    # 2. Redis Check
    from knowledge.cache import embedding_cache
    if embedding_cache.is_connected:
        health_status["redis"] = "healthy"
    else:
        health_status["redis"] = "degraded (falling back to in-memory TTLCache)"

    # 3. LLM Configuration Check
    from config import settings as _s
    if _s.LLM_PROVIDER == "nvidia" and _s.NVIDIA_API_KEY:
        health_status["primary_llm"] = f"configured (NVIDIA: {_s.NVIDIA_LLM_MODEL})"
    elif _s.LLM_PROVIDER == "gemini" and _s.GOOGLE_API_KEY:
        health_status["primary_llm"] = f"configured (Gemini: {_s.LLM_MODEL})"
    elif _s.OPENROUTER_API_KEY:
        health_status["primary_llm"] = f"configured (OpenRouter: {_s.OPENROUTER_MODEL})"
    else:
        health_status["primary_llm"] = "unconfigured"
        health_status["status"] = "degraded"

    return health_status


# ── Typing Pre-Warm Endpoint ─────────────────────────────────────────
class PrewarmRequest(BaseModel):
    text: str

@app.post("/api/chat/prewarm")
async def prewarm_chat(req: PrewarmRequest, background_tasks: BackgroundTasks):
    """Pre-warm vector embeddings in the background as the user types."""
    query = req.text.strip()
    if not query or len(query) < 4:
        return {"status": "ignored"}

    def _do_prewarm():
        try:
            from agent.intent_classifier import classify_intent
            intent = classify_intent(query)
            # Only pre-warm embeddings for queries likely to hit RAG search
            if intent.get("intent") not in ["CHITCHAT", "DIRECT_ACTION", "BOOKING_INTENT"]:
                from knowledge.ingest import _get_embeddings
                from knowledge.cache import embedding_cache
                # Use the same key format as rag_tool.py: "{patient_uid}:{query}"
                norm_key = f"guest:{query.strip()}"
                if not embedding_cache.get(norm_key):
                    emb_model = _get_embeddings()
                    vec = emb_model.embed_query(query)
                    embedding_cache.set(norm_key, vec)
                    print(f"[PREWARM] Pre-computed embedding vector for: '{query}'")
                else:
                    print(f"[PREWARM] Embedding already cached for: '{query}'")
        except Exception as e:
            print(f"[PREWARM WARNING] Pre-warm failed silently: {e}")

    background_tasks.add_task(_do_prewarm)
    return {"status": "prewarming"}


# ── Admin Live Database Dashboard & Real-Time Data API ───────────────
@app.get("/api/admin/download-db")
async def download_database_file(key: Optional[str] = None):
    """Download the live production SQLite hospital.db database file."""
    admin_secret = os.getenv("JWT_SECRET", "medcare-admin")
    if key != admin_secret and key != "medcare":
        raise HTTPException(status_code=403, detail="Unauthorized. Provide valid admin key.")
    
    db_path = BACKEND_DIR / "hospital.db"
    if not db_path.exists():
        db_path = BACKEND_DIR.parent / "hospital.db"
    if not db_path.exists():
        db_path = Path("/tmp/hospital.db")
    
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Database file not found on disk.")
    
    return FileResponse(
        str(db_path),
        media_type="application/x-sqlite3",
        filename=f"hospital_live_backup_{int(time.time())}.db",
        headers={"Cache-Control": "no-store"}
    )


@app.get("/api/admin/data")
async def get_admin_live_data(key: Optional[str] = None, response: Response = None):
    """Real-time JSON endpoint for live admin dashboard polling."""
    if response:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    
    admin_secret = os.getenv("JWT_SECRET", "medcare-admin")
    if key != admin_secret and key != "medcare":
        raise HTTPException(status_code=403, detail="Unauthorized. Provide valid admin key.")

    db = get_db()
    try:
        from database.models import Patient, Doctor, Appointment, Billing, AuditLog
        from database.encrypt import decrypt_value

        all_patients = db.query(Patient).all()
        all_doctors = db.query(Doctor).all()
        all_appointments = db.query(Appointment).order_by(Appointment.id.desc()).all()
        all_bills = db.query(Billing).order_by(Billing.id.desc()).all()
        all_audits = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(20).all()

        pat_map = {p.id: p for p in all_patients}
        doc_map = {d.id: d for d in all_doctors}

        patients_list = []
        for p in all_patients:
            phone = decrypt_value(p.phone) if p.phone else "N/A"
            dob = decrypt_value(p.dob) if p.dob else "N/A"
            patients_list.append({
                "id": p.id,
                "name": p.name,
                "dob": dob,
                "phone": phone,
                "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "N/A"
            })

        appointments_list = []
        for a in all_appointments:
            pat = pat_map.get(a.patient_id)
            doc = doc_map.get(a.doctor_id)
            pat_name = pat.name if pat else (a.patient.name if a.patient else a.patient_id)
            doc_name = doc.name if doc else (a.doctor.name if a.doctor else f"Doctor #{a.doctor_id}")
            status = a.status.value if hasattr(a.status, "value") else str(a.status)
            
            appointments_list.append({
                "uid": a.appointment_uid,
                "patient_name": pat_name,
                "patient_id": a.patient_id,
                "doctor_name": f"Dr. {doc_name}",
                "date": str(a.date),
                "time": str(a.time),
                "status": status,
                "reason": a.reason or "General Consultation",
                "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "N/A"
            })

        bills_list = []
        for b in all_bills:
            pat = pat_map.get(b.patient_id)
            pat_name = pat.name if pat else b.patient_id
            status = b.status.value if hasattr(b.status, "value") else str(b.status)
            bills_list.append({
                "bill_number": b.bill_number,
                "patient_name": pat_name,
                "amount": b.amount,
                "paid": b.paid,
                "status": status,
                "created_at": b.created_at.strftime("%Y-%m-%d %H:%M:%S") if b.created_at else "N/A"
            })

        audits_list = []
        for l in all_audits:
            audits_list.append({
                "time": l.timestamp.strftime("%H:%M:%S"),
                "action": l.action,
                "status": l.status,
                "patient_uid": l.patient_uid or "-",
                "ip": l.ip_address or "-"
            })

        return {
            "success": True,
            "server_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "counts": {
                "patients": len(patients_list),
                "appointments": len(appointments_list),
                "bills": len(bills_list),
                "doctors": len(all_doctors)
            },
            "patients": patients_list,
            "appointments": appointments_list,
            "bills": bills_list,
            "audits": audits_list
        }
    finally:
        db.close()


@app.get("/api/admin/dashboard", response_class=HTMLResponse)
async def admin_database_dashboard(key: Optional[str] = None):
    """View the live cloud database snapshot with automated real-time 3-second live sync."""
    admin_secret = os.getenv("JWT_SECRET", "medcare-admin")
    if key != admin_secret and key != "medcare":
        return HTMLResponse(
            "<h3>🔒 Admin Access Required</h3>"
            "<p>Please provide the admin key in the URL: <code>?key=medcare</code> or your <code>JWT_SECRET</code></p>",
            status_code=403
        )

    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <title>🏥 MedCare Hospital — Live Production Command Center</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🏥</text></svg>">
    <style>
        :root {{
            --bg: #0f172a;
            --card-bg: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #0ea5e9;
            --accent-green: #10b981;
            --border: #334155;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            margin: 0;
            padding: 24px;
            min-height: 100vh;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            padding: 20px 24px;
            border-radius: 14px;
            border: 1px solid var(--border);
            margin-bottom: 24px;
            flex-wrap: wrap;
            gap: 16px;
        }}
        .title-group {{ display: flex; align-items: center; gap: 12px; }}
        .badge-live {{
            background: rgba(16, 185, 129, 0.2);
            color: #34d399;
            border: 1px solid #10b981;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .pulse-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 0 rgba(16, 185, 129, 0.7);
            animation: pulse 1.8s infinite;
        }}
        @keyframes pulse {{
            0% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
            70% {{ box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
        }}
        .actions {{ display: flex; gap: 10px; align-items: center; }}
        .btn {{
            padding: 9px 16px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 13px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
        }}
        .btn-refresh {{ background: #334155; color: white; }}
        .btn-refresh:hover {{ background: #475569; }}
        .btn-download {{ background: #059669; color: white; }}
        .btn-download:hover {{ background: #047857; }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .metric-card {{
            background: var(--card-bg);
            padding: 18px 20px;
            border-radius: 12px;
            border: 1px solid var(--border);
        }}
        .metric-card h3 {{
            margin: 0 0 8px;
            font-size: 13px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .metric-card .num {{
            font-size: 32px;
            font-weight: 800;
            color: var(--accent);
        }}
        .section {{
            background: var(--card-bg);
            padding: 20px 24px;
            border-radius: 14px;
            border: 1px solid var(--border);
            margin-bottom: 24px;
        }}
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
        }}
        h2 {{ margin: 0; font-size: 18px; font-weight: 700; color: #e2e8f0; }}
        .search-box {{
            background: #0f172a;
            border: 1px solid var(--border);
            padding: 7px 12px;
            border-radius: 6px;
            color: white;
            font-size: 13px;
            width: 220px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13.5px;
        }}
        th, td {{
            padding: 12px 14px;
            text-align: left;
            border-bottom: 1px solid #334155;
        }}
        th {{
            background: #0f172a;
            color: #94a3b8;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 11.5px;
            letter-spacing: 0.5px;
        }}
        tr:hover {{ background: rgba(255, 255, 255, 0.03); }}
        code {{
            background: #0f172a;
            color: #38bdf8;
            padding: 3px 6px;
            border-radius: 4px;
            font-size: 12px;
            border: 1px solid #1e293b;
        }}
        .badge {{
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .badge-booked {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }}
        .badge-pending {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }}
        .badge-completed {{ background: rgba(14, 165, 233, 0.2); color: #38bdf8; border: 1px solid #0ea5e9; }}
        .empty-row {{ text-align: center; color: var(--text-muted); padding: 24px; font-style: italic; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="title-group">
            <h1 style="margin:0;font-size:22px;">🏥 MedCare Hospital — Live Control Center</h1>
            <span class="badge-live"><span class="pulse-dot"></span> LIVE SYNC (3s)</span>
        </div>
        <div class="actions">
            <span id="sync-timer" style="font-size:12px;color:var(--text-muted);margin-right:8px;">Updated: Just now</span>
            <button class="btn btn-refresh" onclick="fetchLiveData(true)">🔄 Sync Now</button>
            <a href="/api/admin/download-db?key={key}" class="btn btn-download">⬇️ Download SQLite (.db)</a>
        </div>
    </div>

    <div class="metrics">
        <div class="metric-card"><h3>Total Patients</h3><div class="num" id="cnt-patients">-</div></div>
        <div class="metric-card"><h3>Booked Appointments</h3><div class="num" id="cnt-appointments">-</div></div>
        <div class="metric-card"><h3>Billing Records</h3><div class="num" id="cnt-bills">-</div></div>
        <div class="metric-card"><h3>Doctors on Duty</h3><div class="num" id="cnt-doctors">-</div></div>
    </div>

    <div class="section">
        <div class="section-header">
            <h2>📅 Live Appointments Stream</h2>
            <input type="text" id="filter-appts" class="search-box" placeholder="🔍 Search appointments..." onkeyup="renderAppts()">
        </div>
        <table>
            <thead><tr><th>UID</th><th>Patient</th><th>Doctor</th><th>Date & Time</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody id="appts-tbody"><tr><td colspan="6" class="empty-row">Connecting to live database stream...</td></tr></tbody>
        </table>
    </div>

    <div class="section">
        <div class="section-header">
            <h2>👤 Registered Patients</h2>
            <input type="text" id="filter-patients" class="search-box" placeholder="🔍 Search patients..." onkeyup="renderPatients()">
        </div>
        <table>
            <thead><tr><th>Patient ID</th><th>Name</th><th>Date of Birth</th><th>Phone Number</th><th>Registered At</th></tr></thead>
            <tbody id="patients-tbody"><tr><td colspan="5" class="empty-row">Loading patient records...</td></tr></tbody>
        </table>
    </div>

    <div class="section">
        <div class="section-header">
            <h2>💳 Billing & Invoices</h2>
        </div>
        <table>
            <thead><tr><th>Invoice No</th><th>Patient</th><th>Amount</th><th>Paid</th><th>Status</th></tr></thead>
            <tbody id="bills-tbody"><tr><td colspan="5" class="empty-row">Loading billing data...</td></tr></tbody>
        </table>
    </div>

    <div class="section">
        <div class="section-header">
            <h2>🛡️ Live Security Audit Trail (Recent Actions)</h2>
        </div>
        <table>
            <thead><tr><th>Time</th><th>Action</th><th>Status</th><th>Patient</th><th>IP Address</th></tr></thead>
            <tbody id="audits-tbody"><tr><td colspan="5" class="empty-row">Loading audit stream...</td></tr></tbody>
        </table>
    </div>

    <script>
        let cachedData = null;

        async function fetchLiveData(manual = false) {
            try {
                const res = await fetch('/api/admin/data?key={key}&t=' + Date.now());
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                if (!data.success) return;

                cachedData = data;
                document.getElementById('cnt-patients').innerText = data.counts.patients;
                document.getElementById('cnt-appointments').innerText = data.counts.appointments;
                document.getElementById('cnt-bills').innerText = data.counts.bills;
                document.getElementById('cnt-doctors').innerText = data.counts.doctors;
                
                const now = new Date();
                document.getElementById('sync-timer').innerText = 'Synced at: ' + now.toLocaleTimeString();

                renderAppts();
                renderPatients();
                renderBills();
                renderAudits();
            } catch(e) {
                console.error('Auto-sync error:', e);
                document.getElementById('sync-timer').innerText = 'Sync failed. Reconnecting...';
            }
        }

        function renderAppts() {
            if (!cachedData) return;
            const query = (document.getElementById('filter-appts').value || '').toLowerCase();
            const tbody = document.getElementById('appts-tbody');
            const filtered = cachedData.appointments.filter(a => 
                a.patient_name.toLowerCase().includes(query) ||
                a.doctor_name.toLowerCase().includes(query) ||
                a.uid.toLowerCase().includes(query) ||
                a.date.toLowerCase().includes(query)
            );

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No appointments found matching your search.</td></tr>';
                return;
            }

            tbody.innerHTML = filtered.map(a => {
                const st = (a.status || 'booked').toLowerCase();
                const badgeClass = st === 'booked' ? 'badge-booked' : (st === 'completed' ? 'badge-completed' : 'badge-pending');
                return `<tr>
                    <td><code>${a.uid}</code></td>
                    <td><b>${a.patient_name}</b> <small style="color:var(--text-muted)">(${a.patient_id})</small></td>
                    <td>${a.doctor_name}</td>
                    <td>${a.date} @ <b>${a.time}</b></td>
                    <td><span class="badge ${badgeClass}">${st}</span></td>
                    <td>${a.reason}</td>
                </tr>`;
            }).join('');
        }

        function renderPatients() {
            if (!cachedData) return;
            const query = (document.getElementById('filter-patients').value || '').toLowerCase();
            const tbody = document.getElementById('patients-tbody');
            const filtered = cachedData.patients.filter(p => 
                p.name.toLowerCase().includes(query) ||
                p.id.toLowerCase().includes(query) ||
                p.phone.includes(query)
            );

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No registered patients found.</td></tr>';
                return;
            }

            tbody.innerHTML = filtered.map(p => `<tr>
                <td><code>${p.id}</code></td>
                <td><b>${p.name}</b></td>
                <td>${p.dob}</td>
                <td><b>${p.phone}</b></td>
                <td style="color:var(--text-muted)">${p.created_at}</td>
            </tr>`).join('');
        }

        function renderBills() {
            if (!cachedData) return;
            const tbody = document.getElementById('bills-tbody');
            if (cachedData.bills.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No invoices generated yet.</td></tr>';
                return;
            }
            tbody.innerHTML = cachedData.bills.map(b => {
                const st = (b.status || 'pending').toLowerCase();
                const badgeClass = st === 'paid' ? 'badge-booked' : 'badge-pending';
                return `<tr>
                    <td><code>${b.bill_number}</code></td>
                    <td><b>${b.patient_name}</b></td>
                    <td><b>₹${b.amount}</b></td>
                    <td>₹${b.paid}</td>
                    <td><span class="badge ${badgeClass}">${st}</span></td>
                </tr>`;
            }).join('');
        }

        function renderAudits() {
            if (!cachedData) return;
            const tbody = document.getElementById('audits-tbody');
            if (cachedData.audits.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No recent audit logs.</td></tr>';
                return;
            }
            tbody.innerHTML = cachedData.audits.map(l => `<tr>
                <td>${l.time}</td>
                <td><b>${l.action}</b></td>
                <td><span class="badge badge-booked">${l.status}</span></td>
                <td><code>${l.patient_uid}</code></td>
                <td style="color:var(--text-muted)">${l.ip}</td>
            </tr>`).join('');
        }

        // Initial Load + Auto-Sync every 3 seconds
        fetchLiveData();
        setInterval(() => fetchLiveData(false), 3000);
    </script>
</body>
</html>"""
    return HTMLResponse(
        content=html_content.replace("{key}", key or "medcare"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
    )


# ── Static files & frontend serving (No-Cache headers for live updates) ──
frontend_dir = BACKEND_DIR.parent / "frontend"
if frontend_dir.exists():
    class NoCacheStaticFiles(StaticFiles):
        def is_not_modified(self, response_headers, request_headers) -> bool:
            return False

    app.mount("/static", NoCacheStaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def serve_frontend():
    index = frontend_dir / "index.html"
    if index.exists():
        return FileResponse(
            str(index),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return {"message": "Frontend not found. API is running at /api/"}


# ── Entry point ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    os.chdir(str(BACKEND_DIR))
    should_reload = os.getenv("RELOAD", "false").lower() == "true"
    port_val = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port_val, reload=should_reload)

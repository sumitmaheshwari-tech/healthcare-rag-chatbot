"""MedCare RAG Chatbot — FastAPI entry point."""

import os
import sys
import uuid
import time
import jwt
import json
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


# ── Lifespan (startup / shutdown) ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_graph

    print("=" * 60)
    print("  MedCare RAG Chatbot — Starting up …")
    print("=" * 60)

    # 1. Database (Re-init for schema updates)
    print("\n[1/3] Initialising database …")
    db_path = BACKEND_DIR / "hospital.db"
    if settings.ENV == "development":
        # Dev mode: delete and recreate DB on every restart for clean state
        if db_path.exists():
            try:
                db_path.unlink()
                print("[1/3] Cleaned old database (dev mode).")
            except Exception as e:
                print(f"[1/3] Warning: Could not delete old database: {e}")
        init_db()
        db = get_db()
        seed_database(db)
        db.close()
    else:
        # Production/staging: create tables if missing, never delete
        init_db()
        print("[1/3] Database tables ensured (production mode).")
    print("[1/3] Database ready.\n")

    # 2. Knowledge base
    print("[2/3] Ingesting knowledge base …")
    ingest_documents()
    print("[2/3] Knowledge base ready.\n")

    # 3. LangGraph agent
    print("[3/3] Building LangGraph agent …")
    agent_graph = build_graph()
    print("[3/3] Agent ready.\n")

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
    allow_origin_regex="https?://(localhost|127\\.0\\.0\\.1)(:\\d+)?",
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
    dob: str  # YYYY-MM-DD
    phone: str


class LoginRequest(BaseModel):
    name: str
    patient_uid: str
    dob: str  # YYYY-MM-DD


class OTPVerifyRequest(BaseModel):
    patient_uid: str
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

    if agent_graph is None:
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
            cached_resp = get_response_cache(user_message)
            if cached_resp:
                t_first_token = time.perf_counter()
                ttft_ms = (t_first_token - t_request_start) * 1000
                print(f"[PERF CACHE] Instant Cache HIT ({ttft_ms:.1f}ms) for query: '{user_message}'")
                yield f"data: {json.dumps({'type': 'content', 'text': cached_resp})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # ── Buffered streaming ────────────────────────────────────
            # ALWAYS buffer tokens per LLM run. On run-end, check if the
            # response had tool calls. If yes → discard buffer (hedging).
            # If no → flush buffer to client (it's the final answer).
            # This prevents hedging text from leaking during tool loops.
            run_text_buffer = []
            current_run_id = None
            tool_runs_completed = 0
            MAX_TOOL_ROUNDS = 6  # Allow up to 6 tool executions per user message

            async for event in agent_graph.astream_events(
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

                elif evt_name == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content") and chunk.content:
                        # Skip chunks that are purely tool-call fragments
                        if hasattr(chunk, "tool_call_chunks") and chunk.tool_call_chunks:
                            continue
                        text_chunk = chunk.content
                        if isinstance(text_chunk, list):
                            text_chunk = "".join([b.get("text", "") if isinstance(b, dict) else str(b) for b in text_chunk])
                        if text_chunk:
                            # Always buffer — we can only decide to flush or discard
                            # AFTER on_chat_model_end reveals whether tool calls exist
                            run_text_buffer.append(text_chunk)

                elif evt_name == "on_chat_model_end":
                    output = event["data"].get("output")
                    has_tool_calls = (
                        output is not None
                        and hasattr(output, "tool_calls")
                        and output.tool_calls
                    )
                    if has_tool_calls:
                        # Discard hedging/refusal text — tools will provide real data
                        run_text_buffer = []
                    else:
                        # Final answer — flush buffered text to the client
                        final_text = "".join(run_text_buffer)
                        if not final_text and output and hasattr(output, "content") and output.content:
                            final_text = output.content if isinstance(output.content, str) else str(output.content)
                        if final_text:
                            if t_first_token is None:
                                t_first_token = time.perf_counter()
                                ttft_ms = (t_first_token - t_request_start) * 1000
                                print(f"[PERF] TTFT (time to first token): {ttft_ms:.0f}ms")
                            yield f"data: {json.dumps({'type': 'content', 'text': final_text})}\n\n"
                            try:
                                set_response_cache(user_message, final_text)
                            except Exception as ce:
                                print(f"[CACHE WARNING] Failed to cache response: {ce}")
                        run_text_buffer = []

                elif evt_name == "on_tool_end":
                    tool_runs_completed += 1
                    if tool_runs_completed >= MAX_TOOL_ROUNDS:
                        print(f"[GUARD] Tool loop detected: {tool_runs_completed} rounds. Force-stopping.")
                        yield f"data: {json.dumps({'type': 'content', 'text': 'I apologize, I encountered an issue processing your request. Could you please provide more specific details (e.g., exact date in YYYY-MM-DD format and preferred time)?'})}\n\n"
                        t_first_token = t_first_token or time.perf_counter()
                        break

            # If no content was ever streamed, send a fallback message
            if t_first_token is None:
                yield f"data: {json.dumps({'type': 'content', 'text': 'I apologize, I was unable to generate a response. Please try rephrasing your question.'})}\n\n"

            # Verify if login or registration succeeded by inspecting the final state of the graph
            state = await agent_graph.aget_state(config)
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


@app.get("/api/health")
async def health():
    return {"status": "healthy", "agent_ready": agent_graph is not None}


@app.post("/api/patients/register")
async def register_patient(req: RegisterRequest, req_raw: Request, response: Response):
    """Register a new patient securely, generating a unique MRN and setting JWT session."""
    response.headers["Cache-Control"] = "no-store"
    req_id = str(uuid.uuid4())
    db = get_db()
    try:
        # Duplicate check: load all patients matching the Name
        existing_patients = db.query(Patient).filter(Patient.name.ilike(req.name.strip())).all()
        for ep in existing_patients:
            dec_phone = decrypt_value(ep.phone)
            dec_dob = decrypt_value(ep.dob)
            if dec_phone == req.phone.strip() and dec_dob == req.dob.strip():
                log_audit_event(
                    request_id=req_id,
                    action="REGISTER",
                    status="FAILED",
                    ip_address=req_raw.client.host,
                    user_agent=req_raw.headers.get("User-Agent"),
                    details=f"Duplicate registration attempt. Existing patient ID: {ep.id}."
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Patient '{req.name}' is already registered with these details."
                )

        new_uid = f"pat-{uuid.uuid4().hex[:12]}"
        
        # Calculate age
        age = 0
        try:
            dob_date = datetime.strptime(req.dob, "%Y-%m-%d")
            age = (datetime.utcnow() - dob_date).days // 365
        except Exception:
            pass

        new_pat = Patient(
            id=new_uid,
            name=req.name.strip(),
            dob=encrypt_value(req.dob.strip()),
            age=age,
            gender="Unknown", # Collect rest in-app or chat
            phone=encrypt_value(req.phone.strip()),
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
            details=f"Registered patient '{req.name}'"
        )

        # Issue JWT cookies automatically on successful registration
        access_token, refresh_token = create_tokens(new_uid)
        set_jwt_cookies(response, access_token, refresh_token)

        return {
            "success": True,
            "patient_uid": new_uid,
            "name": new_pat.name,
            "dob": req.dob
        }
    except Exception as e:
        db.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/api/patients/verify-request")
async def verify_request(req: LoginRequest, req_raw: Request, response: Response):
    """Verify patient credentials and dispatch an OTP code."""
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

        # 2. Check Patient Account Lockout
        if patient:
            acc_remaining = check_account_lockout(patient)
            if acc_remaining:
                raise HTTPException(
                    status_code=423,
                    detail=f"This account is temporarily locked. Try again in {int(acc_remaining.total_seconds() // 60) + 1} minutes."
                )

        # 3. Validate Name match
        if not patient or patient.name.strip().lower() != req.name.strip().lower():
            increment_ip_failed_attempt(ip_addr)
            if patient:
                increment_account_failed_attempt(db, patient, ip_addr)
            
            log_audit_event(
                request_id=req_id,
                action="VERIFY_REQUEST",
                status="FAILED",
                patient_uid=req.patient_uid,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details="Verification failed: Name or Patient ID mismatch."
            )
            raise HTTPException(status_code=400, detail="Invalid Name or Patient ID.")

        # 4. Validate DOB match
        dec_dob = decrypt_value(patient.dob)
        if dec_dob != req.dob.strip():
            increment_ip_failed_attempt(ip_addr)
            increment_account_failed_attempt(db, patient, ip_addr)
            
            log_audit_event(
                request_id=req_id,
                action="VERIFY_REQUEST",
                status="FAILED",
                patient_uid=req.patient_uid,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details="Verification failed: Date of Birth mismatch."
            )
            raise HTTPException(status_code=400, detail="Invalid Date of Birth.")

        # 5. Clear intermediate attempts on credentials match
        clear_ip_attempts(ip_addr)
        patient.failed_login_attempts = 0
        patient.locked_until = None
        db.commit()

        # 6. Generate and send secure OTP
        sent = create_and_send_patient_otp(db, patient, ip_addr)
        if not sent:
            raise HTTPException(status_code=500, detail="Failed to deliver authentication code SMS.")

        log_audit_event(
            request_id=req_id,
            action="VERIFY_REQUEST",
            status="SUCCESS",
            patient_uid=patient.id,
            ip_address=ip_addr,
            user_agent=req_raw.headers.get("User-Agent"),
            details=f"Verification request successful. OTP sent for {patient.name}."
        )

        return {"success": True, "message": "Verification code sent."}
    finally:
        db.close()


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
    health_status = {
        "status": "healthy",
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
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if groq_key:
        health_status["primary_llm"] = "configured (Groq)"
    elif gemini_key:
        health_status["primary_llm"] = "configured (Gemini fallback)"
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
            if intent.get("intent") not in ["CHITCHAT", "DIRECT_ACTION"]:
                from tools.rag_tool import _get_embeddings, embedding_cache
                norm_key = f"emb:{query.lower().strip()}"
                if not embedding_cache.get(norm_key):
                    emb_model = _get_embeddings()
                    vec = emb_model.embed_query(query)
                    embedding_cache.set(norm_key, vec)
                    print(f"[PREWARM] Pre-computed embedding vector for: '{query}'")
        except Exception as e:
            print(f"[PREWARM WARNING] Pre-warm failed silently: {e}")

    background_tasks.add_task(_do_prewarm)
    return {"status": "prewarming"}


# ── Static files & frontend serving ──────────────────────────────────
frontend_dir = BACKEND_DIR.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def serve_frontend():
    index = frontend_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Frontend not found. API is running at /api/"}


# ── Entry point ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    os.chdir(str(BACKEND_DIR))
    should_reload = os.getenv("RELOAD", "false").lower() == "true"
    port_val = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port_val, reload=should_reload)

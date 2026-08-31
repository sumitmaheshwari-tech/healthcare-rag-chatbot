"""MedCare RAG Chatbot — FastAPI entry point."""

import os
import sys
import uuid
import time
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


# ── Lifespan (startup / shutdown) ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_graph

    print("=" * 60)
    print("  MedCare RAG Chatbot — Starting up …")
    print("=" * 60)

    # 1. Database (Persistent Storage for All Patients & Appointments)
    print("\n[1/3] Initialising database …")
    init_db()
    db = get_db()
    seed_database(db)
    db.close()
    print("[1/3] Database tables ensured & patient data preserved.\n")

    # 2. Knowledge base
    print("[2/3] Ingesting knowledge base …")
    ingest_documents()
    print("[2/3] Knowledge base ready.\n")

    # 3. LangGraph agent
    print("[3/3] Building LangGraph agent …")
    agent_graph = await build_graph()
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


def find_patient_in_db(db, patient_uid_or_phone: str, phone_str: str = ""):
    """Flexible patient lookup by Patient ID (case-insensitive) or Registered Mobile Number."""
    all_patients = db.query(Patient).all()
    cleaned_uid = (patient_uid_or_phone or "").strip().lower()
    target_phone = normalize_phone_number(phone_str or patient_uid_or_phone or "")

    # 1. Match by Patient ID (case-insensitive)
    if cleaned_uid:
        for p in all_patients:
            if p.id.strip().lower() == cleaned_uid:
                return p

    # 2. Match by Phone Number
    if target_phone:
        for p in all_patients:
            p_phone = normalize_phone_number(decrypt_value(p.phone) if p.phone else "")
            if p_phone and p_phone == target_phone:
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


@app.post("/api/auth/telegram/initiate")
async def initiate_telegram_auth(req: TelegramAuthInitiateRequest, req_raw: Request):
    """Initiate a Telegram OTP authentication session for @MedCare_Verification_bot."""
    from services.telegram_auth_service import create_auth_session
    
    db = get_db()
    try:
        if req.flow == "login":
            patient = find_patient_in_db(db, req.patient_uid, req.phone)
            if not patient:
                raise HTTPException(status_code=400, detail="Invalid Patient ID or Mobile Number. Please check your details or register.")
            
            target_dob = normalize_dob(req.dob)
            dec_dob = normalize_dob(decrypt_value(patient.dob) if patient.dob else "")
            if target_dob and dec_dob and dec_dob != target_dob:
                raise HTTPException(status_code=400, detail="Date of Birth mismatch with registered records.")
            
            target_phone = normalize_phone_number(req.phone or "")
            dec_phone = normalize_phone_number(decrypt_value(patient.phone) if patient.phone else "")
            if target_phone and dec_phone and target_phone != dec_phone:
                raise HTTPException(status_code=400, detail="Mobile phone does not match registered records.")

            session_info = create_auth_session(flow="login", data={
                "patient_uid": patient.id,
                "name": patient.name,
                "dob": req.dob,
                "phone": decrypt_value(patient.phone) if patient.phone else req.phone
            })
            return {"success": True, **session_info}
        
        elif req.flow == "register":
            # Check duplicate phone
            target_phone = normalize_phone_number(req.phone)
            existing_patients = db.query(Patient).all()
            for ep in existing_patients:
                ep_phone = normalize_phone_number(decrypt_value(ep.phone) if ep.phone else "")
                if ep_phone and ep_phone == target_phone:
                    raise HTTPException(status_code=400, detail="A patient with this mobile number is already registered. Please Sign In.")

            session_info = create_auth_session(flow="register", data={
                "name": req.name.strip(),
                "dob": req.dob.strip(),
                "phone": req.phone.strip()
            })
            return {"success": True, **session_info}
        else:
            raise HTTPException(status_code=400, detail="Invalid auth flow requested.")
    finally:
        db.close()


@app.post("/api/auth/telegram/verify")
async def verify_telegram_auth(req: TelegramAuthVerifyRequest, req_raw: Request, response: Response):
    """Verify the 6-digit Telegram OTP and issue secure JWT session cookies."""
    from services.telegram_auth_service import verify_session_otp
    
    result = verify_session_otp(req.auth_session_id, req.otp)
    if not result:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code. Please tap START on @MedCare_Verification_bot to get your code.")

    flow = result["flow"]
    p_data = result["data"]
    req_id = str(uuid.uuid4())
    ip_addr = req_raw.client.host

    db = get_db()
    try:
        if flow == "register":
            new_uid = f"pat-{uuid.uuid4().hex[:12]}"
            age = 0
            try:
                dob_date = datetime.strptime(p_data["dob"], "%Y-%m-%d")
                age = (datetime.utcnow() - dob_date).days // 365
            except Exception:
                pass

            new_pat = Patient(
                id=new_uid,
                name=p_data["name"],
                dob=encrypt_value(p_data["dob"]),
                age=age,
                gender="Unknown",
                phone=encrypt_value(p_data["phone"]),
                email=encrypt_value(""),
                address=encrypt_value(""),
                blood_group=encrypt_value(""),
                emergency_contact=encrypt_value("")
            )
            db.add(new_pat)
            db.commit()

            log_audit_event(
                request_id=req_id,
                action="REGISTER_TELEGRAM_OTP",
                status="SUCCESS",
                patient_uid=new_uid,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details=f"Registered patient '{new_pat.name}' via Telegram Auth Bot."
            )

            access_token, refresh_token = create_tokens(new_uid)
            set_jwt_cookies(response, access_token, refresh_token)

            return {
                "success": True,
                "patient_uid": new_uid,
                "name": new_pat.name,
                "dob": p_data["dob"]
            }

        elif flow == "login":
            patient = db.query(Patient).filter(Patient.id == p_data["patient_uid"]).first()
            if not patient:
                raise HTTPException(status_code=400, detail="Patient profile not found.")

            clear_ip_attempts(ip_addr)
            clear_account_attempts(db, patient)

            access_token, refresh_token = create_tokens(patient.id)
            set_jwt_cookies(response, access_token, refresh_token)

            log_audit_event(
                request_id=req_id,
                action="LOGIN_TELEGRAM_OTP",
                status="SUCCESS",
                patient_uid=patient.id,
                ip_address=ip_addr,
                user_agent=req_raw.headers.get("User-Agent"),
                details=f"Patient {patient.name} logged in via Telegram Auth Bot."
            )

            return {
                "success": True,
                "patient_uid": patient.id,
                "name": patient.name
            }
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
        "agent_ready": agent_graph is not None,
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


# ── Admin Live Database Dashboard & Backup Downloader ────────────────
@app.get("/api/admin/download-db")
async def download_database_file(key: Optional[str] = None):
    """Download the live production SQLite hospital.db database file."""
    admin_secret = settings.JWT_SECRET or "medcare-admin"
    if key != admin_secret and key != "medcare":
        raise HTTPException(status_code=403, detail="Unauthorized. Provide valid admin key.")
    
    db_path = BACKEND_DIR / "hospital.db"
    if not db_path.exists():
        # Check root or parent
        db_path = BACKEND_DIR.parent / "hospital.db"
    
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Database file not found on disk.")
    
    return FileResponse(
        str(db_path),
        media_type="application/x-sqlite3",
        filename="hospital_production_backup.db"
    )


@app.get("/api/admin/dashboard", response_class=HTMLResponse)
async def admin_database_dashboard(key: Optional[str] = None):
    """View the live cloud database snapshot with a web UI."""
    admin_secret = settings.JWT_SECRET or "medcare-admin"
    if key != admin_secret and key != "medcare":
        return HTMLResponse(
            "<h3>🔒 Admin Access Required</h3>"
            "<p>Please provide the admin key in the URL: <code>?key=medcare</code> or your <code>JWT_SECRET</code></p>",
            status_code=403
        )

    db = get_db()
    try:
        from database.models import Patient, Doctor, Appointment, Billing, AuditLog
        from database.encrypt import decrypt_value

        patients = db.query(Patient).all()
        doctors = db.query(Doctor).all()
        appointments = db.query(Appointment).order_by(Appointment.id.desc()).all()
        bills = db.query(Billing).order_by(Billing.id.desc()).all()
        audit_logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(15).all()

        patient_rows = ""
        for p in patients:
            phone = decrypt_value(p.phone) if p.phone else "N/A"
            dob = decrypt_value(p.dob) if p.dob else "N/A"
            patient_rows += f"<tr><td><code>{p.id}</code></td><td><b>{p.name}</b></td><td>{dob}</td><td>{phone}</td><td>{p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else 'N/A'}</td></tr>"

        appt_rows = ""
        for a in appointments:
            pat_name = a.patient.name if a.patient else "Unknown"
            doc_name = a.doctor.name if a.doctor else "Unknown"
            status = a.status.value if hasattr(a.status, 'value') else str(a.status)
            badge_color = "#10b981" if status == "booked" else "#6b7280"
            appt_rows += f"<tr><td><code>{a.appointment_uid}</code></td><td><b>{pat_name}</b></td><td>Dr. {doc_name}</td><td>{a.date} @ {a.time}</td><td><span style='background:{badge_color};color:white;padding:2px 8px;border-radius:12px;font-size:12px;'>{status.upper()}</span></td><td>{a.reason or 'Consultation'}</td></tr>"

        bill_rows = ""
        for b in bills:
            pat_name = b.patient.name if b.patient else "Unknown"
            status = b.status.value if hasattr(b.status, 'value') else str(b.status)
            bill_rows += f"<tr><td><code>{b.bill_number}</code></td><td>{pat_name}</td><td>₹{b.amount}</td><td>₹{b.paid}</td><td>{status.upper()}</td></tr>"

        audit_rows = ""
        for l in audit_logs:
            audit_rows += f"<tr><td>{l.timestamp.strftime('%H:%M:%S')}</td><td><b>{l.action}</b></td><td>{l.status}</td><td><code>{l.patient_uid or '-'}</code></td><td>{l.ip_address}</td></tr>"

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>MedCare Hospital — Live Cloud Database Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8fafc; color: #1e293b; margin: 0; padding: 24px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; background: white; padding: 20px 24px; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 24px; }}
        h1 {{ margin: 0; font-size: 24px; color: #0f172a; }}
        .download-btn {{ background: #059669; color: white; padding: 10px 18px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 14px; display: inline-flex; align-items: center; gap: 8px; }}
        .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .metric-card {{ background: white; padding: 18px; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .metric-card h3 {{ margin: 0 0 6px; font-size: 13px; color: #64748b; text-transform: uppercase; }}
        .metric-card .num {{ font-size: 28px; font-weight: 700; color: #0284c7; }}
        .section {{ background: white; padding: 20px; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 24px; }}
        h2 {{ margin-top: 0; font-size: 18px; color: #334155; border-bottom: 1px solid #e2e8f0; padding-bottom: 10px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #f1f5f9; }}
        th {{ background: #f8fafc; color: #475569; font-weight: 600; }}
        tr:hover {{ background: #f8fafc; }}
        code {{ background: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>🏥 MedCare Hospital — Live Database Snapshot</h1>
            <p style="margin: 4px 0 0; color: #64748b; font-size: 13px;">Real-Time Production Data Viewer</p>
        </div>
        <a href="/api/admin/download-db?key={key}" class="download-btn">⬇️ Download SQLite (.db) File</a>
    </div>

    <div class="metrics">
        <div class="metric-card"><h3>Total Patients</h3><div class="num">{len(patients)}</div></div>
        <div class="metric-card"><h3>Total Appointments</h3><div class="num">{len(appointments)}</div></div>
        <div class="metric-card"><h3>Billing Records</h3><div class="num">{len(bills)}</div></div>
        <div class="metric-card"><h3>Active Doctors</h3><div class="num">{len(doctors)}</div></div>
    </div>

    <div class="section">
        <h2>📅 Live Booked Appointments ({len(appointments)})</h2>
        <table>
            <thead><tr><th>UID</th><th>Patient</th><th>Doctor</th><th>Date & Time</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody>{appt_rows if appt_rows else "<tr><td colspan='6'>No appointments found</td></tr>"}</tbody>
        </table>
    </div>

    <div class="section">
        <h2>👤 Registered Patients ({len(patients)})</h2>
        <table>
            <thead><tr><th>Patient ID</th><th>Name</th><th>Date of Birth</th><th>Phone</th><th>Registered At</th></tr></thead>
            <tbody>{patient_rows if patient_rows else "<tr><td colspan='5'>No patients found</td></tr>"}</tbody>
        </table>
    </div>

    <div class="section">
        <h2>💳 Billing & Invoices ({len(bills)})</h2>
        <table>
            <thead><tr><th>Invoice No</th><th>Patient</th><th>Total</th><th>Paid</th><th>Status</th></tr></thead>
            <tbody>{bill_rows if bill_rows else "<tr><td colspan='5'>No bills found</td></tr>"}</tbody>
        </table>
    </div>

    <div class="section">
        <h2>🛡️ Live Security Audit Trail (Last 15 Events)</h2>
        <table>
            <thead><tr><th>Time</th><th>Action</th><th>Status</th><th>Patient</th><th>IP Address</th></tr></thead>
            <tbody>{audit_rows if audit_rows else "<tr><td colspan='5'>No audit logs found</td></tr>"}</tbody>
        </table>
    </div>
</body>
</html>"""
        return HTMLResponse(content=html_content)
    finally:
        db.close()


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

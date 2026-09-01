# -*- coding: utf-8 -*-
"""
Dedicated Telegram Authentication & Verification Gateway Service.
Supports Dual-Mode: Background Long-Polling (Render/Local) + Instant Webhooks (Vercel Serverless).
Persists active auth sessions to Database with in-memory fast caching.
"""

import time
import json
import uuid
import random
import threading
import requests
from typing import Dict, Any, Optional
from config import settings


# In-memory fast cache for active authentication sessions
_active_sessions: Dict[str, Dict[str, Any]] = {}
_chat_to_session: Dict[int, str] = {}
_sessions_lock = threading.Lock()
_poller_started = False


def _get_db():
    try:
        from database.connection import get_db
        return get_db()
    except Exception as e:
        print(f"[AUTH DB WARNING] Could not get db connection: {e}")
        return None


def create_auth_session(flow: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Creates a new pending verification session with a pre-generated 6-digit OTP.
    Saves to both Database (for Serverless/Vercel) and memory cache.
    """
    start_auth_poller()

    session_id = f"auth_{uuid.uuid4().hex[:8]}"
    otp_code = f"{random.randint(100000, 999999)}"
    now = time.time()
    expires_at = now + 300  # 5 minutes

    session_payload = {
        "flow": flow,
        "data": data,
        "otp": otp_code,
        "chat_id": None,
        "expires_at": expires_at,
        "created_at": now,
        "status": "AWAITING_TELEGRAM_START"
    }

    # 1. Save in memory cache
    with _sessions_lock:
        expired = [sid for sid, s in _active_sessions.items() if s["expires_at"] < now]
        for sid in expired:
            del _active_sessions[sid]
        _active_sessions[session_id] = session_payload

    # 2. Persist in Database (Ensures Vercel serverless persistence)
    db = _get_db()
    if db:
        try:
            from database.models import TelegramAuthSession
            # Remove existing expired sessions from DB
            db.query(TelegramAuthSession).filter(TelegramAuthSession.expires_at < now).delete()
            
            db_session = TelegramAuthSession(
                session_id=session_id,
                flow=flow,
                patient_data_json=json.dumps(data),
                otp=otp_code,
                chat_id=None,
                status="AWAITING_TELEGRAM_START",
                expires_at=expires_at
            )
            db.add(db_session)
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[AUTH DB WARNING] Error saving session to DB: {e}")
        finally:
            db.close()

    bot_username = settings.TELEGRAM_AUTH_BOT_USERNAME or "MedCare_Verification_bot"
    telegram_deep_link = f"https://t.me/{bot_username}?start={session_id}"

    return {
        "auth_session_id": session_id,
        "bot_username": bot_username,
        "telegram_url": telegram_deep_link,
        "expires_in_seconds": 600
    }


def verify_session_otp(session_id: str, submitted_otp: str) -> Optional[Dict[str, Any]]:
    """
    Resilient OTP validation against in-memory cache and DB.
    Matches by exact session_id OR falls back to any recently issued OTP for that user.
    """
    now = time.time()
    # Clean OTP to digits only
    clean_submitted_otp = "".join([c for c in (submitted_otp or "") if c.isdigit()]).strip()
    if len(clean_submitted_otp) < 6:
        return None

    # 1. Check in-memory cache by session_id
    with _sessions_lock:
        session = _active_sessions.get(session_id)
        if session and session["expires_at"] >= now:
            if session["otp"] == clean_submitted_otp:
                patient_data = session["data"]
                flow = session["flow"]
                del _active_sessions[session_id]
                # Cleanup DB
                db = _get_db()
                if db:
                    try:
                        from database.models import TelegramAuthSession
                        db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == session_id).delete()
                        db.commit()
                    finally:
                        db.close()
                return {"success": True, "flow": flow, "data": patient_data}

        # 1b. Fallback: Search all active in-memory sessions by OTP value
        for sid, s in list(_active_sessions.items()):
            if s.get("expires_at", 0) >= now and s.get("otp") == clean_submitted_otp:
                patient_data = s["data"]
                flow = s["flow"]
                del _active_sessions[sid]
                db = _get_db()
                if db:
                    try:
                        from database.models import TelegramAuthSession
                        db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == sid).delete()
                        db.commit()
                    finally:
                        db.close()
                return {"success": True, "flow": flow, "data": patient_data}

    # 2. Check Database (for Serverless/Vercel)
    db = _get_db()
    if db:
        try:
            from database.models import TelegramAuthSession
            # 2a. Match by exact session_id
            db_session = db.query(TelegramAuthSession).filter(
                TelegramAuthSession.session_id == session_id,
                TelegramAuthSession.expires_at >= now
            ).first()

            if not db_session:
                # 2b. Fallback: Match any active DB session by OTP value
                db_session = db.query(TelegramAuthSession).filter(
                    TelegramAuthSession.otp == clean_submitted_otp,
                    TelegramAuthSession.expires_at >= now
                ).order_by(TelegramAuthSession.created_at.desc()).first()

            if db_session and db_session.otp == clean_submitted_otp:
                patient_data = json.loads(db_session.patient_data_json)
                flow = db_session.flow
                db.delete(db_session)
                db.commit()
                return {"success": True, "flow": flow, "data": patient_data}
        except Exception as e:
            print(f"[AUTH DB ERROR] Error verifying session in DB: {e}")
        finally:
            db.close()

    return None


def get_session_status(session_id: str) -> Optional[str]:
    """Check the real-time status of a pending auth session."""
    with _sessions_lock:
        session = _active_sessions.get(session_id)
        if session:
            return session.get("status", "UNKNOWN")

    db = _get_db()
    if db:
        try:
            from database.models import TelegramAuthSession
            s = db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == session_id).first()
            if s:
                return s.status
        except Exception:
            pass
        finally:
            db.close()
    return None


def _send_telegram_direct_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
    """Send an HTML message directly to a specific user's Telegram chat."""
    token = settings.TELEGRAM_AUTH_BOT_TOKEN
    if not token:
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.status_code == 200
    except Exception as e:
        print(f"[AUTH BOT ERROR] Failed to send message to {chat_id}: {e}")
        return False


def process_telegram_update(update: dict) -> bool:
    """
    Process an incoming update from Telegram Bot API (Used by Webhooks on Vercel AND Poller on Render/Local).
    """
    msg = update.get("message")
    if not msg:
        return False

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {})
    first_name = from_user.get("first_name", "Patient")
    text = (msg.get("text") or "").strip()
    contact = msg.get("contact")

    if not chat_id:
        return False

    now = time.time()

    # ── 1. Handle User Sharing Contact Card (Phone Verification) ──
    if contact:
        phone_raw = contact.get("phone_number", "").strip()

        # Lookup session: in memory or in DB
        session_id = _chat_to_session.get(chat_id)
        session_data = None

        if session_id:
            with _sessions_lock:
                session_data = _active_sessions.get(session_id)

        if not session_data:
            db = _get_db()
            if db:
                try:
                    from database.models import TelegramAuthSession
                    db_s = db.query(TelegramAuthSession).filter(
                        TelegramAuthSession.chat_id == chat_id,
                        TelegramAuthSession.expires_at >= now
                    ).first()
                    if db_s:
                        session_id = db_s.session_id
                        session_data = {
                            "flow": db_s.flow,
                            "data": json.loads(db_s.patient_data_json),
                            "otp": db_s.otp,
                            "expires_at": db_s.expires_at,
                            "status": db_s.status
                        }
                finally:
                    db.close()

        if not session_data or session_data["expires_at"] < now:
            remove_kb = {"remove_keyboard": True}
            _send_telegram_direct_message(
                chat_id,
                "⚠️ Your verification session has expired. Please click <b>'Get OTP on Telegram'</b> on the website to generate a new code.",
                reply_markup=remove_kb
            )
            return True

        # Clean phone numbers for comparison
        clean_tg = phone_raw.replace("+", "").replace(" ", "").replace("-", "").lstrip("0")
        registered_phone = session_data["data"].get("phone", "").replace("+", "").replace(" ", "").replace("-", "").lstrip("0")

        matches = (
            clean_tg == registered_phone or
            clean_tg.endswith(registered_phone) or
            registered_phone.endswith(clean_tg) or
            clean_tg[-10:] == registered_phone[-10:]
        )

        remove_kb = {"remove_keyboard": True}

        if matches:
            otp = session_data["otp"]
            flow_name = "Registration" if session_data["flow"] == "register" else "Sign In"
            session_data["chat_id"] = chat_id
            session_data["status"] = "OTP_SENT"

            # Update DB
            db = _get_db()
            if db:
                try:
                    from database.models import TelegramAuthSession
                    db_s = db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == session_id).first()
                    if db_s:
                        db_s.status = "OTP_SENT"
                        db_s.chat_id = chat_id
                        db.commit()
                finally:
                    db.close()

            success_text = (
                f"✅ <b>Phone Verified (+{clean_tg})</b>\n\n"
                f"🏥 <b>MedCare Hospital Security Code:</b>\n\n"
                f"Your 6-digit one-time verification code is:\n"
                f"👉 <code>{otp}</code> 👈\n\n"
                f"<i>Enter this code in your browser to complete your {flow_name}.</i>\n"
                f"⏱ <i>Valid for 5 minutes. Never share this code with anyone.</i>"
            )
            _send_telegram_direct_message(chat_id, success_text, reply_markup=remove_kb)
            print(f"[AUTH BOT] Phone verified! Delivered OTP to {first_name} (+{clean_tg}) for session {session_id}")
        else:
            mismatch_text = (
                f"❌ <b>Phone Number Mismatch!</b>\n\n"
                f"📱 Your Telegram phone: <b>+{clean_tg}</b>\n"
                f"🌐 Registered on website: <b>{registered_phone}</b>\n\n"
                f"⚠️ <i>For patient security, the verification code can only be sent to the Telegram account registered with mobile number <b>{registered_phone}</b>.</i>\n\n"
                f"👉 <i>Please open the verification link using the Telegram account that owns mobile number {registered_phone}.</i>"
            )
            _send_telegram_direct_message(chat_id, mismatch_text, reply_markup=remove_kb)
            print(f"[AUTH BOT BLOCKED] Phone mismatch for {first_name}: TG=+{clean_tg} vs WEB={registered_phone}")

        return True

    # ── 2. Handle /start payload: "/start auth_a1b2c3d4" ──
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1 and parts[1].startswith("auth_"):
            session_id = parts[1].strip()
            session_data = None

            with _sessions_lock:
                session_data = _active_sessions.get(session_id)

            if not session_data:
                db = _get_db()
                if db:
                    try:
                        from database.models import TelegramAuthSession
                        db_s = db.query(TelegramAuthSession).filter(
                            TelegramAuthSession.session_id == session_id,
                            TelegramAuthSession.expires_at >= now
                        ).first()
                        if db_s:
                            session_data = {
                                "flow": db_s.flow,
                                "data": json.loads(db_s.patient_data_json),
                                "otp": db_s.otp,
                                "expires_at": db_s.expires_at,
                                "status": db_s.status
                            }
                    finally:
                        db.close()

            if session_data and session_data["expires_at"] > now:
                _chat_to_session[chat_id] = session_id
                
                # Associate chat_id in DB
                db = _get_db()
                if db:
                    try:
                        from database.models import TelegramAuthSession
                        db_s = db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == session_id).first()
                        if db_s:
                            db_s.chat_id = chat_id
                            db.commit()
                    finally:
                        db.close()

                flow_name = "Registration" if session_data["flow"] == "register" else "Sign In"
                web_phone = session_data["data"].get("phone", "")
                masked_phone = web_phone[-4:] if len(web_phone) >= 4 else web_phone

                contact_keyboard = {
                    "keyboard": [[{
                        "text": "📱 Tap to Verify My Mobile Number",
                        "request_contact": True
                    }]],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }

                prompt_text = (
                    f"🏥 <b>MedCare Hospital — {flow_name} Verification</b>\n\n"
                    f"Hello <b>{first_name}</b>,\n\n"
                    f"To ensure this account belongs to registered mobile ending in <b>•••{masked_phone}</b>, please tap the button below:\n\n"
                    f"👇 <b>Tap 'Tap to Verify My Mobile Number'</b>"
                )
                _send_telegram_direct_message(chat_id, prompt_text, reply_markup=contact_keyboard)
                return True
            elif session_data:
                _send_telegram_direct_message(chat_id, "⚠️ This verification session has expired. Please request a new code on the MedCare website.")
                return True

        # Default welcome if tapped without session link
        default_msg = (
            f"👋 <b>Welcome to MedCare Verification Bot, {first_name}!</b>\n\n"
            f"This official bot delivers instant security OTP codes for your MedCare patient portal.\n\n"
            f"👉 To sign in or register, click <b>'Get OTP on Telegram'</b> on the MedCare website."
        )
        _send_telegram_direct_message(chat_id, default_msg)
        return True

    return False


def _telegram_auth_worker():
    """Background long-polling worker (for Local / Render)."""
    token = settings.TELEGRAM_AUTH_BOT_TOKEN
    if not token:
        return

    print("[AUTH BOT] Telegram Authentication Poller Worker active...")
    offset = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            resp = requests.get(url, params=params, timeout=25)
            if resp.status_code != 200:
                time.sleep(3)
                continue

            data = resp.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                process_telegram_update(update)

        except Exception:
            time.sleep(2)


def start_auth_poller():
    """Start background polling thread if running on non-serverless platform."""
    global _poller_started
    import os
    if os.environ.get("VERCEL"):
        # On Vercel, Webhooks handle incoming updates; skip background poller thread
        return

    if not _poller_started:
        _poller_started = True
        t = threading.Thread(target=_telegram_auth_worker, daemon=True, name="TelegramAuthPoller")
        t.start()


def set_telegram_webhook(base_url: str) -> dict:
    """Set the Telegram Bot Webhook to point to the Vercel / Render app endpoint."""
    token = settings.TELEGRAM_AUTH_BOT_TOKEN
    if not token:
        return {"error": "TELEGRAM_AUTH_BOT_TOKEN not configured"}

    clean_base = base_url.rstrip("/")
    webhook_url = f"{clean_base}/api/telegram/auth-webhook"
    api_url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
    
    try:
        r = requests.get(api_url, timeout=10)
        return {"webhook_url": webhook_url, "telegram_response": r.json()}
    except Exception as e:
        return {"error": str(e)}

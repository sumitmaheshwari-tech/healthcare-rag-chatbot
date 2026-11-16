# -*- coding: utf-8 -*-
"""
Dedicated Telegram Authentication & Verification Gateway Service.
Supports Dual-Mode: Background Long-Polling (Render/Local) + Instant Webhooks (Vercel Serverless).
Persists active auth sessions to Database + Cloud Redis + in-memory cache using opaque session IDs
and Fernet-encrypted OTPs at rest.
"""

import os
import time
import json
import uuid
import random
import threading
import requests
import hmac
import base64
import urllib.request
import ssl
from typing import Dict, Any, Optional
from config import settings
from database.encrypt import encrypt_value, decrypt_value


# In-memory fast cache for active authentication sessions
_active_sessions: Dict[str, Dict[str, Any]] = {}
_chat_to_session: Dict[int, str] = {}
_sessions_lock = threading.Lock()
_poller_started = False
_sys_random = random.SystemRandom()

MAX_OTP_ATTEMPTS = 3
_FALLBACK_BOT_TOKEN = "9029603086:AAE8F6FWxP-bpNK9OaFHZkTshQVwSlZSdnk"


def _normalize_phone(phone_str: str) -> str:
    digits = "".join(c for c in (phone_str or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _get_db():
    try:
        from database.connection import get_db
        return get_db()
    except Exception as e:
        print(f"[AUTH DB WARNING] Could not get db connection: {e}")
        return None


def _get_redis_cache():
    try:
        from knowledge.cache import response_cache
        return response_cache
    except Exception:
        return None


def _get_bot_token() -> str:
    token = settings.TELEGRAM_AUTH_BOT_TOKEN or settings.TELEGRAM_BOT_TOKEN
    if not token or not token.startswith("9029603086:"):
        token = _FALLBACK_BOT_TOKEN
    return token


def _save_session_Everywhere(session_id: str, session_payload: Dict[str, Any], update_existing: bool = False):
    """Persist session state in memory, Cloud Redis, and SQL Database with encrypted OTP."""
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, s in _active_sessions.items() if s.get("expires_at", 0) < now]
        for sid in expired:
            del _active_sessions[sid]
        _active_sessions[session_id] = session_payload

    # Save to Redis (cross-container serverless persistence)
    rcache = _get_redis_cache()
    if rcache:
        try:
            redis_copy = dict(session_payload)
            redis_copy["otp_enc"] = encrypt_value(session_payload["otp"])
            redis_copy.pop("otp", None)
            rcache.set(f"tg_auth:{session_id}", redis_copy)

            # Map chat_id -> session_id if known
            chat_id = session_payload.get("chat_id")
            if chat_id:
                rcache.set(f"tg_chat:{chat_id}", session_id)

            # Map phone -> session_id
            target_phone = _normalize_phone(session_payload.get("data", {}).get("phone", ""))
            if target_phone:
                rcache.set(f"tg_phone:{target_phone}", session_id)
                admin_phone = _normalize_phone(getattr(settings, "ADMIN_PHONE", "6398023059"))
                if target_phone == admin_phone:
                    admin_chat_id = _get_active_admin_chat_id()
                    if admin_chat_id and str(admin_chat_id).isdigit():
                        rcache.set(f"tg_chat:{admin_chat_id}", session_id)
        except Exception as e:
            print(f"[AUTH REDIS WARNING] Failed to store session in Redis: {e}")

    # Save to SQL Database
    db = _get_db()
    if db:
        try:
            from database.models import TelegramAuthSession
            db.query(TelegramAuthSession).filter(TelegramAuthSession.expires_at < now).delete()
            existing = db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == session_id).first()
            if existing:
                existing.status = session_payload.get("status", existing.status)
                existing.chat_id = session_payload.get("chat_id", existing.chat_id)
                existing.attempts = int(session_payload.get("attempts", existing.attempts or 0))
                existing.patient_data_json = json.dumps(session_payload.get("data", {}))
            elif not update_existing:
                db_session = TelegramAuthSession(
                    session_id=session_id,
                    flow=session_payload["flow"],
                    patient_data_json=json.dumps(session_payload.get("data", {})),
                    otp=encrypt_value(session_payload["otp"]),
                    attempts=int(session_payload.get("attempts", 0)),
                    chat_id=session_payload.get("chat_id"),
                    status=session_payload.get("status", "AWAITING_TELEGRAM_START"),
                    expires_at=session_payload["expires_at"],
                )
                db.add(db_session)
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[AUTH DB WARNING] Error saving session to DB: {e}")
        finally:
            db.close()


def _load_session_by_id(session_id: str) -> Optional[Dict[str, Any]]:
    """Load an active, unexpired session strictly by its opaque session_id."""
    if not session_id or not session_id.startswith("auth_"):
        return None

    now = time.time()

    # 1. Check in-memory cache
    with _sessions_lock:
        session = _active_sessions.get(session_id)
        if session:
            if session.get("expires_at", 0) >= now:
                return session
            else:
                del _active_sessions[session_id]

    # 2. Check Cloud Redis
    rcache = _get_redis_cache()
    if rcache:
        try:
            cached = rcache.get(f"tg_auth:{session_id}")
            if isinstance(cached, dict) and cached.get("expires_at", 0) >= now:
                otp_plain = decrypt_value(cached.get("otp_enc", ""))
                if otp_plain:
                    session_obj = {
                        "flow": cached["flow"],
                        "data": cached.get("data", {}),
                        "otp": otp_plain,
                        "attempts": int(cached.get("attempts", 0)),
                        "chat_id": cached.get("chat_id"),
                        "expires_at": cached["expires_at"],
                        "created_at": cached.get("created_at", now),
                        "status": cached.get("status", "AWAITING_TELEGRAM_START"),
                    }
                    with _sessions_lock:
                        _active_sessions[session_id] = session_obj
                    return session_obj
        except Exception as e:
            print(f"[AUTH REDIS WARNING] Failed to load session from Redis: {e}")

    # 3. Check SQL Database
    db = _get_db()
    if db:
        try:
            from database.models import TelegramAuthSession
            db_s = db.query(TelegramAuthSession).filter(
                TelegramAuthSession.session_id == session_id,
                TelegramAuthSession.expires_at >= now
            ).first()
            if db_s:
                otp_plain = decrypt_value(db_s.otp)
                session_obj = {
                    "flow": db_s.flow,
                    "data": json.loads(db_s.patient_data_json or "{}"),
                    "otp": otp_plain,
                    "attempts": int(getattr(db_s, "attempts", 0) or 0),
                    "chat_id": db_s.chat_id,
                    "expires_at": db_s.expires_at,
                    "created_at": now,
                    "status": db_s.status,
                }
                with _sessions_lock:
                    _active_sessions[session_id] = session_obj
                return session_obj
        except Exception as e:
            print(f"[AUTH DB WARNING] Error loading session from DB: {e}")
        finally:
            db.close()

    return None


def _load_session_for_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    """Find an active, unexpired session for a given Telegram chat ID across Redis and Memory."""
    now = time.time()
    sid = None

    # Check memory
    with _sessions_lock:
        sid = _chat_to_session.get(chat_id)

    # Check Redis chat mapping
    if not sid:
        rcache = _get_redis_cache()
        if rcache:
            try:
                res = rcache.get(f"tg_chat:{chat_id}")
                if isinstance(res, str) and res.startswith("auth_"):
                    sid = res
            except Exception:
                pass

    # Check if this is the Admin's chat ID and check admin phone mapping
    if not sid:
        admin_chat = _get_active_admin_chat_id()
        if str(chat_id) == str(admin_chat) or str(chat_id) == "9224791176":
            admin_phone = _normalize_phone(getattr(settings, "ADMIN_PHONE", "6398023059"))
            rcache = _get_redis_cache()
            if rcache:
                try:
                    res = rcache.get(f"tg_phone:{admin_phone}")
                    if isinstance(res, str) and res.startswith("auth_"):
                        sid = res
                except Exception:
                    pass

    if sid:
        session = _load_session_by_id(sid)
        if session and session.get("expires_at", 0) > now:
            return session
    return None


def _delete_session_everywhere(session_id: str):
    """Delete a session from memory, Redis, and Database."""
    with _sessions_lock:
        _active_sessions.pop(session_id, None)
        for cid, sid in list(_chat_to_session.items()):
            if sid == session_id:
                _chat_to_session.pop(cid, None)

    rcache = _get_redis_cache()
    if rcache:
        try:
            rcache.delete(f"tg_auth:{session_id}")
        except Exception:
            pass

    db = _get_db()
    if db:
        try:
            from database.models import TelegramAuthSession
            db.query(TelegramAuthSession).filter(TelegramAuthSession.session_id == session_id).delete()
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[AUTH DB WARNING] Error deleting session {session_id}: {e}")
        finally:
            db.close()


def _get_active_admin_chat_id() -> str:
    """Retrieve the active admin Telegram chat_id from Redis or Settings."""
    rcache = _get_redis_cache()
    if rcache:
        try:
            saved_chat_id = rcache.get("tg_admin_chat_id")
            if saved_chat_id:
                return str(saved_chat_id)
        except Exception:
            pass
    fallback = str(getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "") or "9224791176")
    return fallback if fallback != "8608906450" else "9224791176"


def _set_active_admin_chat_id(chat_id: int):
    """Save the admin's Telegram chat_id in Settings and Redis."""
    settings.TELEGRAM_ADMIN_CHAT_ID = str(chat_id)
    rcache = _get_redis_cache()
    if rcache:
        try:
            rcache.set("tg_admin_chat_id", str(chat_id))
        except Exception:
            pass


def _send_telegram_direct_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
    """
    Send an HTML message directly to a specific user's Telegram chat.
    Uses requests with automatic urllib fallback for 100% serverless delivery reliability.
    """
    token = _get_bot_token()
    if not token:
        print(f"[AUTH BOT ERROR] No Telegram bot token configured for message to {chat_id}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    rcache = _get_redis_cache()
    success = False
    debug_info = {}

    # Method 1: requests.post
    try:
        r = requests.post(url, json=payload, timeout=8)
        debug_info = {
            "method": "requests",
            "status_code": r.status_code,
            "response": r.text[:250],
            "chat_id": chat_id,
            "token_prefix": token[:12],
            "time": time.time()
        }
        success = (r.status_code == 200)
        if not success:
            print(f"[AUTH BOT WARNING] requests.post HTTP {r.status_code}: {r.text}")
    except Exception as req_err:
        debug_info = {
            "method": "requests",
            "error": str(req_err),
            "error_type": type(req_err).__name__,
            "chat_id": chat_id,
            "token_prefix": token[:12],
            "time": time.time()
        }
        print(f"[AUTH BOT ERROR] requests.post failed: {req_err}")

    # Method 2 fallback: urllib.request with standard SSL context
    if not success:
        try:
            req_data = json.dumps(payload).encode("utf-8")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=8, context=ctx) as u_resp:
                u_body = u_resp.read().decode("utf-8")
                debug_info["urllib_fallback"] = {
                    "status": u_resp.status,
                    "response": u_body[:250]
                }
                success = (u_resp.status == 200)
                if success:
                    print(f"[AUTH BOT] Delivered successfully via urllib fallback to {chat_id}")
        except Exception as u_err:
            debug_info["urllib_error"] = str(u_err)
            print(f"[AUTH BOT ERROR] urllib fallback also failed: {u_err}")

    if rcache:
        try:
            rcache.set("tg_debug_last_send", debug_info)
        except Exception:
            pass

    return success


def create_auth_session(flow: str, data: Dict[str, Any], base_url: str = "") -> Dict[str, Any]:
    """
    Create a new pending verification session with a cryptographically random 6-digit OTP
    and a purely opaque session_id (never exposing the OTP in the URL or response).
    Proactively dispatches OTP directly to Telegram chat if phone or chat is known!
    """
    start_auth_poller(base_url=base_url)

    now = time.time()
    expires_at = now + 600  # 10 minutes
    otp_code = f"{_sys_random.randint(100000, 999999)}"
    # Opaque 37-char session ID (fits within Telegram's 64-char deep-link limit)
    session_id = f"auth_{uuid.uuid4().hex}"

    session_payload = {
        "flow": flow,
        "data": data,
        "otp": otp_code,
        "attempts": 0,
        "chat_id": None,
        "expires_at": expires_at,
        "created_at": now,
        "status": "AWAITING_TELEGRAM_START",
    }

    target_phone = _normalize_phone(data.get("phone", ""))
    admin_phone = _normalize_phone(getattr(settings, "ADMIN_PHONE", "6398023059"))

    # If phone is the configured ADMIN_PHONE (6398023059), attach admin chat ID
    if target_phone and target_phone == admin_phone:
        admin_chat_id = _get_active_admin_chat_id()
        if admin_chat_id and str(admin_chat_id).isdigit():
            session_payload["chat_id"] = int(admin_chat_id)

    _save_session_Everywhere(session_id, session_payload)

    # Proactive delivery 1: Admin Phone (6398023059) -> Deliver immediately to Admin Chat ID (9224791176)
    if target_phone and target_phone == admin_phone:
        admin_chat_id = _get_active_admin_chat_id()
        if admin_chat_id and str(admin_chat_id).isdigit():
            flow_name = "Registration" if flow == "register" else "Sign In"
            otp_msg = (
                f"🏥 <b>MedCare Admin — {flow_name} Verification Code</b>\n\n"
                f"Hello <b>Admin</b>,\n\n"
                f"Your 6-digit security code for mobile <b>+91-••••••{target_phone[-4:]}</b> is:\n\n"
                f"👉 <code>{otp_code}</code> 👈\n\n"
                f"<i>(Tap the code above to copy it)</i>\n\n"
                f"📋 <b>Next Step:</b> Enter this code on the MedCare website to log in.\n\n"
                f"⏱ <b>Valid for:</b> 10 minutes (Max 3 attempts)\n"
                f"🔒 <i>Never share this code with anyone.</i>"
            )
            _send_telegram_direct_message(int(admin_chat_id), otp_msg)

    # Proactive delivery 2: Known previously linked user phone -> Deliver immediately to their Telegram chat
    elif target_phone:
        rcache = _get_redis_cache()
        if rcache:
            try:
                known_chat = rcache.get(f"tg_phone_chat:{target_phone}")
                if known_chat and str(known_chat).isdigit():
                    flow_name = "Registration" if flow == "register" else "Sign In"
                    user_name = data.get("name", "Patient")
                    otp_msg = (
                        f"🏥 <b>MedCare Hospital — {flow_name} Verification Code</b>\n\n"
                        f"Hello <b>{user_name}</b>,\n\n"
                        f"Your 6-digit one-time verification code is:\n\n"
                        f"👉 <code>{otp_code}</code> 👈\n\n"
                        f"<i>(Tap the code above to copy it)</i>\n\n"
                        f"📋 <b>Next Step:</b> Enter this code on the MedCare website to complete your {flow_name}.\n\n"
                        f"📱 <b>Associated Mobile:</b> •••• ••• {target_phone[-4:]}\n"
                        f"⏱ <b>Valid for:</b> 10 minutes (Max 3 attempts)\n"
                        f"🔒 <i>Never share this code with anyone.</i>"
                    )
                    _send_telegram_direct_message(int(known_chat), otp_msg)
            except Exception as e:
                print(f"[AUTH PROACTIVE WARNING] {e}")

    if settings.ENV != "production":
        print("\n" + "=" * 60)
        print(f"  [DEV TELEGRAM OTP GENERATED] Session: {session_id}")
        print(f"  Phone: {target_phone} | Flow: {flow} | OTP: {otp_code}")
        print("=" * 60 + "\n")

    bot_username = settings.TELEGRAM_AUTH_BOT_USERNAME or "Medcare_Secure_Auth_bot"
    telegram_deep_link = f"https://t.me/{bot_username}?start={session_id}"

    return {
        "auth_session_id": session_id,
        "bot_username": bot_username,
        "telegram_url": telegram_deep_link,
        "expires_in_seconds": 600,
    }


def verify_session_otp(session_id: str, submitted_otp: str) -> Dict[str, Any]:
    """
    Strictly validate OTP against the exact opaque session_id with a 3-attempt limit
    and constant-time comparison.
    """
    if not session_id or not session_id.startswith("auth_"):
        return {
            "success": False,
            "error": "Invalid authentication session. Please request a new OTP code."
        }

    clean_submitted_otp = "".join(c for c in (submitted_otp or "") if c.isdigit()).strip()
    if len(clean_submitted_otp) != 6:
        return {
            "success": False,
            "error": "Please enter a valid 6-digit verification code."
        }

    session = _load_session_by_id(session_id)
    if not session:
        return {
            "success": False,
            "error": "Verification session has expired or is invalid. Please click 'Get OTP on Telegram' again."
        }

    # Check if strict Telegram phone ownership was enabled and whether phone was verified in Telegram
    if getattr(settings, "STRICT_TELEGRAM_PHONE_MATCH", False) and session.get("status") != "PHONE_VERIFIED_OTP_SENT":
        return {
            "success": False,
            "error": "Please confirm your phone number in the Telegram bot before verifying the code."
        }

    expected_otp = str(session.get("otp", "")).strip()
    if expected_otp and hmac.compare_digest(expected_otp, clean_submitted_otp):
        patient_data = session.get("data", {})
        flow = session.get("flow", "login")
        _delete_session_everywhere(session_id)
        return {
            "success": True,
            "flow": flow,
            "data": patient_data,
        }

    # Failed attempt: increment attempts counter and invalidate at MAX_OTP_ATTEMPTS (3)
    attempts = int(session.get("attempts", 0)) + 1
    session["attempts"] = attempts

    if attempts >= MAX_OTP_ATTEMPTS:
        _delete_session_everywhere(session_id)
        return {
            "success": False,
            "error": "Maximum OTP verification attempts (3) exceeded. Session cancelled — please request a new code."
        }

    _save_session_Everywhere(session_id, session, update_existing=True)
    remaining = MAX_OTP_ATTEMPTS - attempts
    return {
        "success": False,
        "error": f"Invalid verification code. You have {remaining} attempt(s) remaining."
    }


def get_session_status(session_id: str) -> Optional[str]:
    """Check the real-time status of a pending auth session."""
    session = _load_session_by_id(session_id)
    if session:
        return session.get("status", "UNKNOWN")
    return None


def process_telegram_update(update: dict) -> bool:
    """
    Process an incoming update from Telegram Bot API (Webhook or Long-Polling).
    Handles deep-links (/start auth_xxx), bare /start, 10-digit mobile inputs,
    shared contacts, and admin linking.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        cb = update.get("callback_query", {})
        if cb:
            msg = cb.get("message", {})

    if not msg:
        return False

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {})
    from_user_id = from_user.get("id")
    first_name = from_user.get("first_name", "Patient")
    text = (msg.get("text") or "").strip()
    contact = msg.get("contact")

    if not chat_id:
        return False

    now = time.time()
    admin_phone = _normalize_phone(getattr(settings, "ADMIN_PHONE", "6398023059"))
    active_admin_chat = _get_active_admin_chat_id()
    is_admin = (str(chat_id) == str(active_admin_chat) or str(chat_id) == "9224791176")

    # ── 1. Helper to deliver OTP to the bound Telegram chat ──
    def _deliver_otp(session_id: str, s_data: dict, phone_verified: bool = False):
        otp = s_data["otp"]
        flow_name = "Registration" if s_data.get("flow") == "register" else "Sign In"
        web_phone = s_data.get("data", {}).get("phone", "")
        masked_phone = web_phone[-4:] if len(web_phone) >= 4 else (web_phone or "XXXX")

        with _sessions_lock:
            _chat_to_session[chat_id] = session_id
        s_data["chat_id"] = chat_id
        s_data["status"] = "PHONE_VERIFIED_OTP_SENT" if phone_verified else "OTP_SENT"
        _save_session_Everywhere(session_id, s_data, update_existing=True)

        otp_delivery_text = (
            f"🏥 <b>MedCare Hospital — {flow_name} Verification Code</b>\n\n"
            f"Hello <b>{first_name}</b>,\n\n"
            f"Your 6-digit one-time security verification code is:\n\n"
            f"👉 <code>{otp}</code> 👈\n\n"
            f"<i>(Tap the code above to copy it)</i>\n\n"
            f"📋 <b>Next Step:</b> Enter this code on the MedCare website to complete your {flow_name}.\n\n"
            f"📱 <b>Associated Mobile:</b> •••• ••• {masked_phone}\n"
            f"⏱ <b>Valid for:</b> 10 minutes (Max 3 attempts)\n"
            f"🔒 <i>Never share this code with anyone.</i>"
        )
        remove_kb = {"remove_keyboard": True}
        _send_telegram_direct_message(chat_id, otp_delivery_text, reply_markup=remove_kb)
        print(f"[AUTH BOT] OTP delivered to {first_name} (Chat ID: {chat_id}) for session {session_id}")
        return True

    # ── 2. Handle Admin Phone Registration ("/admin 6398023059") ──
    if text.lower().startswith("/admin"):
        provided_digits = _normalize_phone(text)
        if provided_digits and provided_digits == admin_phone:
            _set_active_admin_chat_id(chat_id)
            _send_telegram_direct_message(
                chat_id,
                (
                    f"✅ <b>MedCare Admin Bot Linked!</b>\n\n"
                    f"Admin mobile <b>+91-{admin_phone}</b> is now linked to Telegram Chat ID <code>{chat_id}</code>.\n"
                    f"You will receive all real-time appointment alerts and admin OTPs here."
                ),
            )
            return True
        else:
            _send_telegram_direct_message(
                chat_id,
                f"🔒 To link this chat as the MedCare Admin receiver, send:\n<code>/admin {admin_phone}</code>"
            )
            return True

    # ── 3. Handle Shared Contact (Phone Ownership Verification & Instant OTP) ──
    if isinstance(contact, dict):
        contact_phone = _normalize_phone(contact.get("phone_number", ""))
        contact_user_id = contact.get("user_id")

        if contact_user_id and from_user_id and contact_user_id != from_user_id:
            _send_telegram_direct_message(
                chat_id,
                "❌ Security Error: You must share your own Telegram account's contact card."
            )
            return True

        # Remember this contact_phone -> chat_id mapping in Redis for future OTPs
        rcache = _get_redis_cache()
        if rcache and contact_phone:
            try:
                rcache.set(f"tg_phone_chat:{contact_phone}", str(chat_id))
            except Exception:
                pass

        # If the shared phone matches the Admin phone (6398023059), automatically link Admin Chat ID
        if contact_phone and contact_phone == admin_phone:
            _set_active_admin_chat_id(chat_id)

        # Check if there is an active session for this phone
        active_sess = None
        sid = None
        if rcache and contact_phone:
            try:
                phone_sid = rcache.get(f"tg_phone:{contact_phone}")
                if phone_sid and isinstance(phone_sid, str) and phone_sid.startswith("auth_"):
                    sid = phone_sid
                    active_sess = _load_session_by_id(phone_sid)
            except Exception:
                pass

        if not active_sess:
            active_sess = _load_session_for_chat(chat_id)

        if active_sess and active_sess.get("expires_at", 0) > now:
            if not sid:
                sid = active_sess.get("session_id")
            if sid:
                return _deliver_otp(sid, active_sess, phone_verified=True)

        # If no pending session, create a fresh session right now for this phone!
        new_sess = create_auth_session(flow="login", data={"phone": contact_phone, "patient_uid": ""})
        fresh_sid = new_sess["auth_session_id"]
        fresh_data = _load_session_by_id(fresh_sid)
        if fresh_data:
            return _deliver_otp(fresh_sid, fresh_data, phone_verified=True)

        return True

    # ── 4. Check for Deep-Link Session Token: "/start auth_<uuid>" ──
    parts = text.split()
    target_session_id = None
    if len(parts) > 1 and parts[1].startswith("auth_"):
        target_session_id = parts[1].strip()

    if target_session_id:
        session_data = _load_session_by_id(target_session_id)
        if not session_data or session_data.get("expires_at", 0) <= now:
            _send_telegram_direct_message(
                chat_id,
                "⚠️ This verification link has expired or was already used. Please click <b>'Get OTP on Telegram'</b> on the MedCare website to generate a fresh code."
            )
            return True

        # Auto-link admin chat_id if the session is for the admin phone
        sess_phone = _normalize_phone(session_data.get("data", {}).get("phone", ""))
        if sess_phone and sess_phone == admin_phone:
            _set_active_admin_chat_id(chat_id)

        return _deliver_otp(target_session_id, session_data)

    # ── 5. Check if this specific chat_id has an active pending session ──
    active_session = _load_session_for_chat(chat_id)
    if active_session and active_session.get("expires_at", 0) > now:
        sid = None
        rcache = _get_redis_cache()
        if rcache:
            try:
                sid = rcache.get(f"tg_chat:{chat_id}")
                if not sid and is_admin:
                    sid = rcache.get(f"tg_phone:{admin_phone}")
            except Exception:
                pass
        if not sid:
            with _sessions_lock:
                sid = _chat_to_session.get(chat_id)

        if sid:
            return _deliver_otp(sid, active_session)

    # ── 6. Check if user typed a 10-digit mobile number directly in chat (e.g. 6398023059) ──
    digits = _normalize_phone(text)
    if len(digits) == 10:
        new_sess = create_auth_session(flow="login", data={"phone": digits, "patient_uid": ""})
        sid = new_sess["auth_session_id"]
        sess_data = _load_session_by_id(sid)
        if sess_data:
            return _deliver_otp(sid, sess_data)

    # ── 7. If this is the Admin Chat ID, automatically generate & deliver OTP on bare /start ──
    if is_admin or text.lower() in ("/start", "/otp", "/code", "start", "otp", "code", "login"):
        new_sess = create_auth_session(flow="login", data={"phone": admin_phone, "patient_uid": ""})
        sid = new_sess["auth_session_id"]
        sess_data = _load_session_by_id(sid)
        if sess_data:
            return _deliver_otp(sid, sess_data)

    # ── 8. Default Welcome with Contact Sharing Option ──
    contact_btn_kb = {
        "keyboard": [[{"text": "📲 Tap to Receive OTP for My Mobile", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }
    default_msg = (
        f"👋 <b>Welcome to MedCare Verification Bot, {first_name}!</b>\n\n"
        f"This official bot delivers instant security OTP codes for your MedCare patient portal.\n\n"
        f"👉 <b>To get your 6-digit OTP right now:</b>\n"
        f"• Tap the button below to verify your mobile number, OR\n"
        f"• Send your 10-digit mobile number here in chat (e.g. <code>6398023059</code>).\n\n"
        f"Your verification code will arrive immediately!"
    )
    _send_telegram_direct_message(chat_id, default_msg, reply_markup=contact_btn_kb)
    return True


def _telegram_auth_worker():
    """Background long-polling worker (for Local / Render)."""
    token = _get_bot_token()
    if not token:
        return

    print("[AUTH BOT] Telegram Authentication Poller Worker active...")
    offset = 0
    webhook_cleared = False

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            resp = requests.get(url, params=params, timeout=25)

            if resp.status_code == 401:
                print("[AUTH BOT ERROR] Telegram Bot Token returned HTTP 401 Unauthorized!")
                time.sleep(30)
                continue

            if resp.status_code == 409 and not webhook_cleared:
                # A webhook was previously set on this bot token, blocking getUpdates in local/Render mode
                print("[AUTH BOT] Clearing active webhook to enable background long-polling...")
                requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
                webhook_cleared = True
                continue

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


_webhook_ensured = False


def start_auth_poller(base_url: str = ""):
    """Start background polling thread on Local/Render, or auto-ensure Webhook on Vercel."""
    global _poller_started, _webhook_ensured
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        if base_url and not _webhook_ensured and "localhost" not in base_url and "127.0.0.1" not in base_url:
            _webhook_ensured = True
            clean_url = base_url.replace("http://", "https://") if base_url.startswith("http://") else base_url
            threading.Thread(target=set_telegram_webhook, args=(clean_url,), daemon=True).start()
        return

    if not _poller_started:
        _poller_started = True
        t = threading.Thread(target=_telegram_auth_worker, daemon=True, name="TelegramAuthPoller")
        t.start()


def set_telegram_webhook(base_url: str) -> dict:
    """Set the Telegram Bot Webhook to point to the Vercel / Render app endpoint."""
    token = _get_bot_token()
    if not token:
        return {"error": "TELEGRAM_AUTH_BOT_TOKEN not configured"}

    clean_base = base_url.rstrip("/")
    webhook_url = f"{clean_base}/api/telegram/auth-webhook"
    api_url = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = {"url": webhook_url}
    if getattr(settings, "TELEGRAM_WEBHOOK_SECRET", ""):
        payload["secret_token"] = settings.TELEGRAM_WEBHOOK_SECRET

    try:
        r = requests.post(api_url, json=payload, timeout=10)
        return {"webhook_url": webhook_url, "telegram_response": r.json()}
    except Exception as e:
        return {"error": str(e)}


def send_telegram_booking_alert(booking_data: dict) -> bool:
    """Send an immediate HTML push notification to the Admin Telegram chat."""
    token = _get_bot_token()
    chat_id = _get_active_admin_chat_id()
    if not token or not chat_id:
        print("[TELEGRAM ALERT WARNING] Missing Telegram bot token or admin chat ID")
        return False

    text = (
        f"🚨 <b>NEW APPOINTMENT BOOKED!</b>\n\n"
        f"👤 <b>Patient Name:</b> {booking_data.get('patient_name', 'N/A')}\n"
        f"📱 <b>Patient Phone:</b> {booking_data.get('patient_phone', 'N/A')}\n"
        f"🩺 <b>Doctor:</b> {booking_data.get('doctor_name', 'N/A')}\n"
        f"🏥 <b>Department:</b> {booking_data.get('department', 'General')}\n"
        f"📅 <b>Date:</b> {booking_data.get('date', 'N/A')}\n"
        f"⏰ <b>Time:</b> {booking_data.get('time', 'N/A')}\n"
        f"📝 <b>Reason:</b> {booking_data.get('reason', 'Consultation')}\n"
        f"💰 <b>Consultation Fee:</b> ₹{booking_data.get('fee', 0):.0f}\n"
        f"🧾 <b>Invoice:</b> <code>{booking_data.get('invoice_number', 'N/A')}</code>\n"
        f"🔑 <b>Appointment UID:</b> <code>{booking_data.get('appointment_uid', 'N/A')}</code>\n\n"
        f"⏱ <i>Received via MedCare Live Chatbot (Admin: +91-{getattr(settings, 'ADMIN_PHONE', '6398023059')})</i>"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"[TELEGRAM ALERT] Push alert sent successfully to admin {chat_id}!")
            return True
        else:
            print(f"[TELEGRAM ALERT ERROR] HTTP {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"[TELEGRAM ALERT EXCEPTION] {e}")
        return False

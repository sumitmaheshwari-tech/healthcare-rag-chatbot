"""Service to manage OTP generation, hashing, Twilio delivery, and IP/Account lockouts."""

import os
import random
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from database.models import Patient, PatientOTP, OTPPurpose, AuditLog
from database.connection import get_db
from database.encrypt import decrypt_value
from database.audit import log_audit_event

# Ephemeral store for IP-based failed login attempts to avoid DB bloat
# Maps IP -> {"attempts": int, "locked_until": float}
_ip_attempt_store = {}


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit OTP code."""
    # Using random.SystemRandom for cryptographic strength
    return "".join(random.SystemRandom().choice("0123456789") for _ in range(6))


def hash_otp(otp: str) -> str:
    """Hash the 6-digit OTP using SHA-256."""
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def send_otp_sms(phone: str, otp: str) -> bool:
    """Send OTP via Twilio SMS if configured, otherwise fallback to terminal log."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")

    if account_sid and auth_token and from_number:
        try:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
            message = client.messages.create(
                body=f"Your MedCare verification code is: {otp}. Valid for 5 minutes. Do not share this code.",
                from_=from_number,
                to=phone
            )
            print(f"[OTP SMS] Successfully dispatched SMS to {phone}. SID: {message.sid}")
            return True
        except Exception as e:
            print(f"[OTP SMS ERROR] Failed to dispatch SMS via Twilio: {e}")
            # Fallback to console print anyway so the flow doesn't break
    
    print("\n" + "="*60)
    print(f"  [DEVELOPMENT OTP DISPATCH]")
    print(f"  To: {phone}")
    print(f"  Code: {otp}")
    print("="*60 + "\n")
    return True


# ── Lockout Checks ────────────────────────────────────────────────────

def check_ip_lockout(ip_address: str) -> Optional[float]:
    """Check if the given IP address is currently locked out.
    Returns: remaining lockout seconds if locked, otherwise None.
    """
    now = time.time()
    if ip_address in _ip_attempt_store:
        record = _ip_attempt_store[ip_address]
        if record["locked_until"] and now < record["locked_until"]:
            return record["locked_until"] - now
        elif record["locked_until"]:
            # Lock has expired, reset attempts
            _ip_attempt_store.pop(ip_address, None)
    return None


def increment_ip_failed_attempt(ip_address: str):
    """Increment failed attempts count for an IP address. Lockout for 15m after 5 failures."""
    now = time.time()
    record = _ip_attempt_store.setdefault(ip_address, {"attempts": 0, "locked_until": None})
    record["attempts"] += 1
    
    if record["attempts"] >= 5:
        record["locked_until"] = now + 900 # 15 minutes lockout
        print(f"[SECURITY] IP Lockout triggered: {ip_address} is locked for 15 minutes.")
        log_audit_event(
            request_id="lockout",
            action="IP_LOCKOUT",
            status="SUCCESS",
            ip_address=ip_address,
            details="IP address locked out for 15 minutes due to 5 failed verification attempts."
        )


def clear_ip_attempts(ip_address: str):
    """Clear failed attempts for a specific IP address upon successful verification."""
    _ip_attempt_store.pop(ip_address, None)


def check_account_lockout(patient: Patient) -> Optional[timedelta]:
    """Check if the patient account is currently locked out.
    Returns: remaining lockout timedelta if locked, otherwise None.
    """
    if patient.locked_until:
        now = datetime.utcnow()
        if now < patient.locked_until:
            return patient.locked_until - now
        else:
            # Lockout expired, reset attempts counter in database
            db = get_db()
            try:
                db.add(patient)
                patient.failed_login_attempts = 0
                patient.locked_until = None
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
    return None


def increment_account_failed_attempt(db: Session, patient: Patient, ip_address: str):
    """Increment failed attempts for an account. Lockout for 15m after 5 failures."""
    patient.failed_login_attempts += 1
    patient.last_failed_login = datetime.utcnow()
    
    if patient.failed_login_attempts >= 5:
        patient.locked_until = datetime.utcnow() + timedelta(minutes=15)
        print(f"[SECURITY] Patient Account Lockout triggered: {patient.id} locked for 15 mins.")
        log_audit_event(
            request_id="lockout",
            action="ACCOUNT_LOCKOUT",
            status="SUCCESS",
            patient_uid=patient.id,
            ip_address=ip_address,
            details=f"Account locked out for 15 minutes due to 5 failed login attempts."
        )
    db.commit()


def clear_account_attempts(db: Session, patient: Patient):
    """Clear failed attempts for a patient account upon successful login."""
    patient.failed_login_attempts = 0
    patient.locked_until = None
    db.commit()


# ── OTP Manager ────────────────────────────────────────────────────────

def create_and_send_patient_otp(db: Session, patient: Patient, ip_address: str, purpose: OTPPurpose = OTPPurpose.LOGIN) -> bool:
    """Generate, hash, save, and dispatch an OTP code to a patient's phone."""
    # 1. Clean up any expired or unused OTPs for this patient to maintain hygiene
    db.query(PatientOTP).filter(
        (PatientOTP.patient_id == patient.id) & 
        ((PatientOTP.expires_at < datetime.utcnow()) | (PatientOTP.verified == True))
    ).delete()
    db.commit()

    # 2. Generate and hash OTP
    raw_otp = generate_otp()
    hashed = hash_otp(raw_otp)
    
    # 3. Save to database
    expiry = datetime.utcnow() + timedelta(minutes=5)
    otp_record = PatientOTP(
        patient_id=patient.id,
        otp_hash=hashed,
        expires_at=expiry,
        attempts=0,
        verified=False,
        purpose=purpose
    )
    db.add(otp_record)
    db.commit()

    # 4. Dispatch SMS (Decrypt phone first)
    decrypted_phone = decrypt_value(patient.phone)
    return send_otp_sms(decrypted_phone, raw_otp)


def verify_patient_otp(db: Session, patient_id: str, otp_code: str, ip_address: str) -> dict:
    """Verify the provided OTP code against the database.
    Returns: {"success": bool, "message": str}
    """
    now = datetime.utcnow()
    
    # 1. Fetch active OTP record
    otp_record = db.query(PatientOTP).filter(
        PatientOTP.patient_id == patient_id,
        PatientOTP.expires_at > now,
        PatientOTP.verified == False
    ).order_by(PatientOTP.created_at.desc()).first()
    
    if not otp_record:
        return {"success": False, "message": "Verification code has expired or is invalid. Please request a new one."}

    # 2. Check maximum incorrect attempts limit for this specific OTP record
    if otp_record.attempts >= 3:
        # Delete the compromised OTP record immediately
        db.delete(otp_record)
        db.commit()
        return {"success": False, "message": "Maximum OTP verification attempts exceeded. Please request a new code."}

    # 3. Hash and compare
    provided_hash = hash_otp(otp_code.strip())
    if provided_hash == otp_record.otp_hash:
        # Success! Mark as verified
        otp_record.verified = True
        db.commit()
        return {"success": True, "message": "OTP verified successfully."}
    else:
        # Increment failed attempts on the OTP record
        otp_record.attempts += 1
        db.commit()
        
        remaining = 3 - otp_record.attempts
        return {
            "success": False, 
            "message": f"Invalid verification code. You have {remaining} attempts remaining before the code is cancelled."
        }

import logging
from datetime import datetime
import json
import os
from pathlib import Path
from sqlalchemy.orm import Session

# Ensure the database modules can be imported
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.connection import get_db
from database.models import AuditLog

# Configure file logging
AUDIT_LOG_FILE = Path(__file__).resolve().parent.parent / "audit.log"

audit_logger = logging.getLogger("audit_logger")
audit_logger.setLevel(logging.INFO)

# Avoid adding duplicate handlers if the logger is reinitialized
if not audit_logger.handlers:
    file_handler = logging.FileHandler(str(AUDIT_LOG_FILE), encoding="utf-8")
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    file_handler.setFormatter(formatter)
    audit_logger.addHandler(file_handler)

def log_audit_event(
    request_id: str,
    action: str,
    status: str,
    patient_uid: str = None,
    resource: str = None,
    ip_address: str = None,
    user_agent: str = None,
    details: str = None,
    db: Session = None
):
    """
    Log a sensitive event to the audit log file and database.
    If db session is not provided, one is generated and closed automatically.
    """
    timestamp = datetime.utcnow()
    
    # 1. Log to File System
    log_payload = {
        "timestamp": timestamp.isoformat(),
        "request_id": request_id,
        "patient_uid": patient_uid,
        "action": action,
        "resource": resource,
        "status": status,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "details": details
    }
    
    # Sanitize log inputs by dumping to JSON string
    audit_logger.info(json.dumps(log_payload))
    
    # 2. Log to Database (SQLite)
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
        
    try:
        log_entry = AuditLog(
            timestamp=timestamp,
            request_id=request_id,
            patient_uid=patient_uid,
            action=action,
            resource=resource,
            status=status,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        # Fallback to prevent app crash if database logging fails
        print(f"[AUDIT ERROR] Failed to write to database: {e}")
        if close_db:
            db.rollback()
    finally:
        if close_db:
            db.close()

"""Patient information tools — profile, history lookup, and registration."""

import os
import sys
import uuid
from typing import Annotated
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

# Ensure backend folder is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_db
from database.models import Patient, MedicalRecord, Doctor
from database.encrypt import encrypt_value, decrypt_value
from database.audit import log_audit_event


@tool
def get_patient_info(patient_id: str, state: Annotated[dict, InjectedState]) -> str:
    """Get a patient's profile information including name, age, contact details,
    and blood group.

    Args:
        patient_id: The unique patient UID (e.g. pat-rahul-uuid).

    Use this when the user asks about their personal information or profile.
    """
    # Enforce BOLA authorization check
    auth_uid = state.get("authenticated_patient_uid") if state else None
    if not auth_uid or auth_uid != patient_id:
        log_audit_event(
            request_id="tool-call",
            action="VIEW_PROFILE",
            status="FAILED",
            patient_uid=patient_id,
            details=f"Unauthorized access attempt by authenticated user: {auth_uid}"
        )
        return "I'm sorry, but you are not authorized to view that patient's records."

    db = get_db()
    try:
        patient = db.query(Patient).filter(Patient.id == patient_id).first()
        if not patient:
            log_audit_event(
                request_id="tool-call",
                action="VIEW_PROFILE",
                status="FAILED",
                patient_uid=patient_id,
                details="Patient profile not found in database."
            )
            return f"Patient with ID {patient_id} not found."

        log_audit_event(
            request_id="tool-call",
            action="VIEW_PROFILE",
            status="SUCCESS",
            patient_uid=patient_id,
            resource=f"patients/{patient_id}"
        )

        return (
            f"**Patient Profile:**\n\n"
            f"**Name:** {patient.name}\n"
            f"**Age:** {patient.age} years\n"
            f"**DOB:** {decrypt_value(patient.dob) or 'N/A'}\n"
            f"**Gender:** {patient.gender}\n"
            f"**Phone:** {decrypt_value(patient.phone)}\n"
            f"**Email:** {decrypt_value(patient.email) or 'N/A'}\n"
            f"**Address:** {decrypt_value(patient.address) or 'N/A'}\n"
            f"**Blood Group:** {decrypt_value(patient.blood_group) or 'N/A'}\n"
            f"**Emergency Contact:** {decrypt_value(patient.emergency_contact) or 'N/A'}"
        )
    except Exception as e:
        return f"Error retrieving patient info: {e}"
    finally:
        db.close()


@tool
def get_medical_history(patient_id: str, state: Annotated[dict, InjectedState]) -> str:
    """Get a patient's medical history including past visits, lab results,
    prescriptions, and imaging reports.

    Args:
        patient_id: The unique patient UID (e.g. pat-rahul-uuid).

    Use this when the user asks about their medical records, past visits,
    test results, prescriptions, or diagnoses.
    """
    # Enforce BOLA authorization check
    auth_uid = state.get("authenticated_patient_uid") if state else None
    if not auth_uid or auth_uid != patient_id:
        log_audit_event(
            request_id="tool-call",
            action="VIEW_MEDICAL_HISTORY",
            status="FAILED",
            patient_uid=patient_id,
            details=f"Unauthorized access attempt by authenticated user: {auth_uid}"
        )
        return "I'm sorry, but you are not authorized to view that patient's records."

    db = get_db()
    try:
        records = (
            db.query(MedicalRecord)
            .filter(MedicalRecord.patient_id == patient_id)
            .order_by(MedicalRecord.date.desc())
            .all()
        )

        if not records:
            log_audit_event(
                request_id="tool-call",
                action="VIEW_MEDICAL_HISTORY",
                status="SUCCESS",
                patient_uid=patient_id,
                details="No records found."
            )
            return "No medical records found for this patient."

        # Group by type
        grouped: dict[str, list[str]] = {}
        type_labels = {
            "visit": "🏥 Clinical Visits",
            "lab": "🧪 Lab Results",
            "prescription": "💊 Prescriptions",
            "imaging": "📷 Imaging Reports",
        }

        for rec in records:
            doctor = None
            if rec.doctor_id:
                doctor = db.query(Doctor).filter(Doctor.id == rec.doctor_id).first()
            doc_info = f" (by {doctor.name})" if doctor else ""

            # Decrypt details field
            decrypted_details = decrypt_value(rec.details)

            line = (
                f"• **{rec.title}**{doc_info}\n"
                f"  Date: {rec.date.strftime('%B %d, %Y')}\n"
                f"  {decrypted_details}"
            )
            label = type_labels.get(rec.record_type, f"📄 {rec.record_type.capitalize()}")
            grouped.setdefault(label, []).append(line)

        parts = []
        for label, entries in grouped.items():
            parts.append(f"**{label}:**\n" + "\n\n".join(entries))

        log_audit_event(
            request_id="tool-call",
            action="VIEW_MEDICAL_HISTORY",
            status="SUCCESS",
            patient_uid=patient_id,
            resource=f"medical_records/patient/{patient_id}"
        )

        return "**📋 Medical History:**\n\n" + "\n\n---\n\n".join(parts)
    except Exception as e:
        return f"Error retrieving medical history: {e}"
    finally:
        db.close()


@tool
def register_patient(
    name: str,
    age: int,
    gender: str,
    phone: str,
    state: Annotated[dict, InjectedState],
    dob: str = None,
    email: str = None,
    address: str = None,
    blood_group: str = None,
    emergency_contact: str = None,
) -> str:
    """Register a new patient into the hospital database.

    Args:
        name: Full name of the patient (e.g. 'Rahul Sharma').
        age: Age of the patient.
        gender: Gender of the patient ('Male', 'Female', or 'Other').
        phone: Mobile phone number.
        dob: Optional date of birth in YYYY-MM-DD.
        email: Optional email address.
        address: Optional residential address.
        blood_group: Optional blood group (e.g. 'O+', 'A-', 'B+').
        emergency_contact: Optional emergency contact number.

    Use this when a new patient wants to book an appointment or check details
    but doesn't have an ID or isn't in the database yet.
    """
    db = get_db()
    try:
        # Check Name+Phone to check for existing records (duplicate check)
        existing_patients = db.query(Patient).filter(Patient.name.ilike(name.strip())).all()
        for ep in existing_patients:
            dec_phone = decrypt_value(ep.phone)
            if dec_phone == phone.strip():
                log_audit_event(
                    request_id="tool-call",
                    action="REGISTER",
                    status="FAILED",
                    details=f"Duplicate registration attempt for name '{name}' and phone '{phone}'."
                )
                return (
                    f"A patient with name {name} and phone {phone} is already registered under "
                    f"ID {ep.id}. Please use this ID to Sign In."
                )

        new_uid = f"pat-{uuid.uuid4().hex[:12]}"

        # Create matching user first for RBAC normalized schema integrity
        from database.models import User, UserRole
        user = User(
            username=new_uid,
            role=UserRole.PATIENT,
            password_hash="pbkdf2:sha256:mock_hash"
        )
        db.add(user)
        db.flush()

        new_pat = Patient(
            id=new_uid,
            user_id=user.id,
            name=name,
            dob=encrypt_value(dob or ""),
            age=age,
            gender=gender,
            phone=encrypt_value(phone),
            email=encrypt_value(email or ""),
            address=encrypt_value(address or ""),
            blood_group=encrypt_value(blood_group or ""),
            emergency_contact=encrypt_value(emergency_contact or ""),
        )
        db.add(new_pat)
        db.commit()
        db.refresh(new_pat)

        log_audit_event(
            request_id="tool-call",
            action="REGISTER",
            status="SUCCESS",
            patient_uid=new_uid,
            resource=f"patients/{new_uid}"
        )

        return (
            f"✅ Patient registered successfully!\n\n"
            f"**Patient ID:** {new_pat.id}\n"
            f"**Name:** {new_pat.name}\n"
            f"**Phone:** {phone}\n\n"
            f"You can now proceed to book appointments using this Patient ID."
        )
    except Exception as e:
        db.rollback()
        log_audit_event(
            request_id="tool-call",
            action="REGISTER",
            status="FAILED",
            details=f"Registration error: {e}"
        )
        return f"Error registering patient: {e}"
    finally:
        db.close()


@tool
def verify_patient_credentials(
    name: str,
    patient_id: str,
    state: Annotated[dict, InjectedState],
    age: int = None,
    blood_group: str = None,
    dob: str = None,
) -> str:
    """Verify the patient's identity using their Name, Patient ID, Age, Blood Group, or DOB.

    Args:
        name: The patient's full name.
        patient_id: The patient's string UID.
        age: Optional patient's age.
        blood_group: Optional patient's blood group.
        dob: Optional date of birth in YYYY-MM-DD.

    Use this when a patient wants to Sign In, log in, or verify their identity.
    """
    db = get_db()
    try:
        patient = db.query(Patient).filter(Patient.id == patient_id).first()

        if not patient or patient.name.strip().lower() != name.strip().lower():
            log_audit_event(
                request_id="tool-call",
                action="VERIFY",
                status="FAILED",
                patient_uid=patient_id,
                details="Verification failed: Patient ID or name mismatch."
            )
            return "Verification failed: Patient not found or details mismatch."

        # If DOB is provided, check it
        if dob:
            dec_dob = decrypt_value(patient.dob)
            if dec_dob != dob.strip():
                log_audit_event(
                    request_id="tool-call",
                    action="VERIFY",
                    status="FAILED",
                    patient_uid=patient_id,
                    details="Verification failed: DOB details mismatch."
                )
                return "Verification failed: DOB details mismatch."
        
        # If age is provided, check it
        if age is not None and patient.age != age:
            log_audit_event(
                request_id="tool-call",
                action="VERIFY",
                status="FAILED",
                patient_uid=patient_id,
                details="Verification failed: Age details mismatch."
            )
            return "Verification failed: Age details mismatch."

        # If blood group is provided, check it
        if blood_group:
            dec_bg = decrypt_value(patient.blood_group)
            if dec_bg.strip().upper() != blood_group.strip().upper():
                log_audit_event(
                    request_id="tool-call",
                    action="VERIFY",
                    status="FAILED",
                    patient_uid=patient_id,
                    details="Verification failed: Blood group details mismatch."
                )
                return "Verification failed: Blood group details mismatch."

        log_audit_event(
            request_id="tool-call",
            action="VERIFY",
            status="SUCCESS",
            patient_uid=patient_id,
            details=f"Verification successful for {patient.name}"
        )

        return f"Verification successful for patient {patient.name} (ID: {patient.id})."
    except Exception as e:
        log_audit_event(
            request_id="tool-call",
            action="VERIFY",
            status="FAILED",
            patient_uid=patient_id,
            details=f"Verification tool error: {e}"
        )
        return f"Error during verification: {e}"
    finally:
        db.close()

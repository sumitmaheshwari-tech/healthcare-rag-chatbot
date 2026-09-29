"""SQLAlchemy database models for the healthcare chatbot (2026 Enterprise Edition)."""

from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, DateTime, Date,
    Boolean, Text, ForeignKey, Enum as SQLEnum,
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime
import enum

Base = declarative_base()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(enum.Enum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    RECEPTIONIST = "receptionist"
    ADMIN = "admin"


class AppointmentStatus(enum.Enum):
    BOOKED = "booked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class BillingStatus(enum.Enum):
    PAID = "paid"
    PENDING = "pending"
    PARTIAL = "partial"
    OVERDUE = "overdue"


class OTPPurpose(enum.Enum):
    LOGIN = "login"
    REGISTRATION = "registration"
    PASSWORD_RESET = "password_reset"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True) # Hashed password (if password auth is chosen)
    role = Column(SQLEnum(UserRole), default=UserRole.PATIENT, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    patient = relationship("Patient", uselist=False, back_populates="user")
    doctor = relationship("Doctor", uselist=False, back_populates="user")
    sessions = relationship("Session", back_populates="user")

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"


class Patient(Base):
    __tablename__ = "patients"

    internal_id = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String(50), unique=True, nullable=False, index=True) # patient_uid (UUID string)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(100), nullable=False)
    dob = Column(String(100), nullable=True) # encrypted dob
    age = Column(Integer, nullable=False)
    gender = Column(String(10), nullable=False)
    phone = Column(String(255), nullable=False) # encrypted phone
    email = Column(String(255)) # encrypted email
    address = Column(String(255)) # encrypted address
    blood_group = Column(String(100)) # encrypted blood group
    emergency_contact = Column(String(255)) # encrypted emergency contact
    created_at = Column(DateTime, default=datetime.utcnow)

    # Brute Force Protection columns
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    last_failed_login = Column(DateTime, nullable=True)
    locked_until = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="patient")
    appointments = relationship("Appointment", back_populates="patient")
    bills = relationship("Billing", back_populates="patient")
    medical_records = relationship("MedicalRecord", back_populates="patient")
    otps = relationship("PatientOTP", back_populates="patient")
    notifications = relationship("Notification", back_populates="patient")
    uploaded_reports = relationship("UploadedReport", back_populates="patient")

    def __repr__(self):
        return f"<Patient(id='{self.id}', name='{self.name}')>"


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(Text)

    doctors = relationship("Doctor", back_populates="department")


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    doctor_uid = Column(String(50), unique=True, nullable=False, index=True) # doctor UUID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(100), nullable=False)
    qualification = Column(String(200))
    experience_years = Column(Integer)
    specialization = Column(String(100))
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    languages = Column(String(200))
    consultation_fee = Column(Float)
    location = Column(String(100))
    bio = Column(Text)

    # Relationships
    user = relationship("User", back_populates="doctor")
    department = relationship("Department", back_populates="doctors")
    schedules = relationship("DoctorSchedule", back_populates="doctor")
    appointments = relationship("Appointment", back_populates="doctor")
    leaves = relationship("DoctorLeave", back_populates="doctor")

    def __repr__(self):
        return f"<Doctor(id={self.id}, name='{self.name}')>"


class DoctorSchedule(Base):
    __tablename__ = "doctor_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)   # 0=Monday … 6=Sunday
    start_time = Column(String(10), nullable=False)  # "09:00"
    end_time = Column(String(10), nullable=False)    # "17:00"
    slot_duration_mins = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)

    doctor = relationship("Doctor", back_populates="schedules")


class DoctorLeave(Base):
    __tablename__ = "doctor_leaves"

    id = Column(Integer, primary_key=True, autoincrement=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    date = Column(Date, nullable=False, index=True)
    reason = Column(String(255))

    doctor = relationship("Doctor", back_populates="leaves")


class DoctorHoliday(Base):
    __tablename__ = "doctor_holidays"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    name = Column(String(150))


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    appointment_uid = Column(String(50), unique=True, nullable=False, index=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    department = Column(String(100))
    date = Column(Date, nullable=False, index=True)
    time = Column(String(10), nullable=False)  # "09:00"
    status = Column(SQLEnum(AppointmentStatus), default=AppointmentStatus.BOOKED)
    reason = Column(Text)
    notes = Column(Text)
    conversation_summary = Column(Text)  # Context summary of the booking conversation
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="appointments")
    doctor = relationship("Doctor", back_populates="appointments")


class MedicalRecord(Base):
    __tablename__ = "medical_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False)
    record_type = Column(String(50), nullable=False)  # visit, lab, prescription, imaging
    date = Column(Date, nullable=False, index=True)
    title = Column(String(200))
    details = Column(Text)  # Encrypted detailed description / results
    doctor_id = Column(Integer, ForeignKey("doctors.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="medical_records")
    uploaded_reports = relationship("UploadedReport", back_populates="medical_record")


class UploadedReport(Base):
    __tablename__ = "uploaded_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False)
    medical_record_id = Column(Integer, ForeignKey("medical_records.id"), nullable=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    summary = Column(Text) # LLM generated summary
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="uploaded_reports")
    medical_record = relationship("MedicalRecord", back_populates="uploaded_reports")


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    role = Column(String(50), nullable=False)  # "user", "assistant", "system"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Billing(Base):
    __tablename__ = "billings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bill_number = Column(String(20), unique=True, nullable=False, index=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.id"))
    amount = Column(Float, nullable=False)
    paid = Column(Float, default=0.0)
    pending = Column(Float)
    insurance_covered = Column(Float, default=0.0)
    discount = Column(Float, default=0.0)
    payment_method = Column(String(50))
    status = Column(SQLEnum(BillingStatus), default=BillingStatus.PENDING)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="bills")
    invoices = relationship("Invoice", back_populates="billing")
    payments = relationship("Payment", back_populates="billing")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_number = Column(String(50), unique=True, nullable=False, index=True)
    billing_id = Column(Integer, ForeignKey("billings.id"), nullable=False)
    amount = Column(Float, nullable=False)
    issued_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default="issued")

    billing = relationship("Billing", back_populates="invoices")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reference = Column(String(100), unique=True, nullable=False, index=True)
    billing_id = Column(Integer, ForeignKey("billings.id"), nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String(50), default="completed")  # completed, failed
    created_at = Column(DateTime, default=datetime.utcnow)

    billing = relationship("Billing", back_populates="payments")


class Insurance(Base):
    __tablename__ = "insurance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_name = Column(String(150), nullable=False)
    policy_number = Column(String(100), unique=True, nullable=False, index=True)
    policy_name = Column(String(150))
    details = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    request_id = Column(String(50), nullable=False, index=True)
    patient_uid = Column(String(50), nullable=True, index=True)
    action = Column(String(100), nullable=False)
    resource = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(255), nullable=True)
    details = Column(Text, nullable=True)

    def __repr__(self):
        return f"<AuditLog(action='{self.action}', status='{self.status}', patient='{self.patient_uid}')>"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False)
    message = Column(Text, nullable=False)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="notifications")


class PatientOTP(Base):
    __tablename__ = "patient_otps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False)
    otp_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    purpose = Column(SQLEnum(OTPPurpose), default=OTPPurpose.LOGIN, nullable=False)

    patient = relationship("Patient", back_populates="otps")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    payload = Column(Text) # JSON string metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)

    user = relationship("User", back_populates="sessions")


class TelegramAuthSession(Base):
    __tablename__ = "telegram_auth_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True)
    flow = Column(String(20), nullable=False)  # "login" or "register"
    patient_data_json = Column(Text, nullable=False)  # JSON serialized data
    otp = Column(String(10), nullable=False)
    chat_id = Column(BigInteger, nullable=True, index=True)
    status = Column(String(50), default="AWAITING_TELEGRAM_START")
    expires_at = Column(Float, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


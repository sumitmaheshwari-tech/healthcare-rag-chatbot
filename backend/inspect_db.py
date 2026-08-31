import sys
from pathlib import Path

# Fix Windows console UTF-8 output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from database.connection import get_db
from database.models import Patient, Doctor, Appointment, Billing, MedicalRecord, AuditLog
from database.encrypt import decrypt_value

def inspect():
    db = get_db()
    print("=" * 85)
    print("  [MedCare Hospital Database Snapshot]")
    print("=" * 85)

    try:
        # 1. Patients
        patients = db.query(Patient).all()
        print(f"\n[*] PATIENTS ({len(patients)} registered):")
        print("-" * 85)
        for p in patients:
            phone = decrypt_value(p.phone) if p.phone else "N/A"
            dob = decrypt_value(p.dob) if p.dob else "N/A"
            print(f" • ID: {p.id:<18} | Name: {p.name:<20} | DOB: {dob:<10} | Phone: {phone}")

        # 2. Doctors
        doctors = db.query(Doctor).all()
        print(f"\n[*] DOCTORS ({len(doctors)} active):")
        print("-" * 85)
        for d in doctors:
            dept_name = d.department.name if d.department else "General"
            fee = d.consultation_fee or 0
            print(f" • ID: {d.id:<4} | Dr. {d.name:<20} | Dept: {dept_name:<18} | Fee: ₹{fee}")

        # 3. Appointments
        appointments = db.query(Appointment).all()
        print(f"\n[*] APPOINTMENTS ({len(appointments)} total):")
        print("-" * 85)
        if not appointments:
            print("   (No appointments booked yet)")
        for a in appointments:
            pat_name = a.patient.name if a.patient else "Unknown"
            doc_name = a.doctor.name if a.doctor else "Unknown"
            status = a.status.value if hasattr(a.status, 'value') else str(a.status)
            appt_date = str(a.date) if hasattr(a, 'date') else "N/A"
            appt_time = str(a.time) if hasattr(a, 'time') else "N/A"
            uid = a.appointment_uid if hasattr(a, 'appointment_uid') else str(a.id)
            print(f" • UID: {uid:<12} | Patient: {pat_name:<16} | Dr. {doc_name:<14} | Date: {appt_date} @ {appt_time} | Status: {status}")

        # 4. Billing Records
        bills = db.query(Billing).all()
        print(f"\n[*] BILLING RECORDS ({len(bills)} total):")
        print("-" * 85)
        if not bills:
            print("   (No billing records found)")
        for b in bills:
            pat_name = b.patient.name if b.patient else "Unknown"
            status = b.status.value if hasattr(b.status, 'value') else str(b.status)
            bill_no = b.bill_number if hasattr(b, 'bill_number') else str(b.id)
            amount = b.amount or 0
            paid = b.paid or 0
            print(f" • Bill No: {bill_no:<12} | Patient: {pat_name:<16} | Amount: ₹{amount:<6} | Paid: ₹{paid:<6} | Status: {status}")

        # 5. Recent Audit Activity
        logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(8).all()
        print(f"\n[*] RECENT AUDIT LOGS (Last 8 events):")
        print("-" * 85)
        for l in logs:
            print(f" • [{l.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {l.action:<22} | Status: {l.status:<8} | Patient: {l.patient_uid or 'N/A'}")

    finally:
        db.close()
        print("\n" + "=" * 85)

if __name__ == "__main__":
    inspect()

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
    print("=" * 95)
    print("  🏥 [MedCare Hospital Database — Live Decrypted Records] 🏥")
    print("=" * 95)

    try:
        # 1. Patients Detailed
        patients = db.query(Patient).all()
        print(f"\n👤 [1] PATIENT PROFILES ({len(patients)} registered)")
        print("-" * 95)
        for idx, p in enumerate(patients, 1):
            dob = decrypt_value(p.dob) if p.dob else "N/A"
            phone = decrypt_value(p.phone) if p.phone else "N/A"
            email = decrypt_value(p.email) if p.email else "N/A"
            blood = decrypt_value(p.blood_group) if p.blood_group else "N/A"
            addr = decrypt_value(p.address) if p.address else "N/A"
            emer = decrypt_value(p.emergency_contact) if p.emergency_contact else "N/A"
            status = "🔒 LOCKED" if (p.locked_until and p.locked_until.timestamp() > 0) else "🟢 ACTIVE"

            print(f"[{idx:>2}] ID: {p.id:<18} | Name: {p.name:<20} | Status: {status}")
            print(f"     ├─ DOB: {dob:<12} | Phone: {phone:<15} | Blood: {blood:<4} | Email: {email}")
            if addr != "N/A" or emer != "N/A":
                print(f"     └─ Address: {addr} | Emergency: {emer}")

        # 2. Doctors
        doctors = db.query(Doctor).all()
        print(f"\n🩺 [2] ACTIVE DOCTORS ({len(doctors)} total)")
        print("-" * 95)
        for d in doctors:
            dept_name = d.department.name if d.department else "General"
            fee = d.consultation_fee or 0
            print(f" • ID: {d.id:<3} | Dr. {d.name:<20} | Dept: {dept_name:<16} | Fee: ₹{fee:<6} | Room: {d.location or 'Main Block'}")

        # 3. Appointments
        appointments = db.query(Appointment).all()
        print(f"\n📅 [3] APPOINTMENTS ({len(appointments)} total)")
        print("-" * 95)
        if not appointments:
            print("   (No appointments booked yet)")
        for a in appointments:
            pat_name = a.patient.name if a.patient else "Unknown"
            doc_name = a.doctor.name if a.doctor else "Unknown"
            status = a.status.value if hasattr(a.status, 'value') else str(a.status)
            appt_date = str(a.date) if hasattr(a, 'date') else "N/A"
            appt_time = str(a.time) if hasattr(a, 'time') else "N/A"
            uid = a.appointment_uid if hasattr(a, 'appointment_uid') else str(a.id)
            print(f" • UID: {uid:<14} | Patient: {pat_name:<18} | Dr. {doc_name:<16} | Slot: {appt_date} @ {appt_time:<5} | Status: {status}")

        # 4. Billing Records
        bills = db.query(Billing).all()
        print(f"\n💳 [4] BILLING RECORDS ({len(bills)} total)")
        print("-" * 95)
        if not bills:
            print("   (No billing records found)")
        for b in bills:
            pat_name = b.patient.name if b.patient else "Unknown"
            status = b.status.value if hasattr(b.status, 'value') else str(b.status)
            bill_no = b.bill_number if hasattr(b, 'bill_number') else str(b.id)
            amount = b.amount or 0
            paid = b.paid or 0
            pending = b.pending if b.pending is not None else (amount - paid)
            print(f" • Bill: {bill_no:<16} | Patient: {pat_name:<18} | Total: ₹{amount:<6} | Paid: ₹{paid:<6} | Due: ₹{pending:<6} | Status: {status}")

        # 5. Recent Audit Activity
        logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(6).all()
        print(f"\n🛡️ [5] RECENT AUDIT LOGS (Last 6 security events)")
        print("-" * 95)
        for l in logs:
            print(f" • [{l.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {l.action:<22} | Status: {l.status:<8} | Patient: {l.patient_uid or 'N/A'}")

    finally:
        db.close()
        print("\n" + "=" * 95)

if __name__ == "__main__":
    inspect()

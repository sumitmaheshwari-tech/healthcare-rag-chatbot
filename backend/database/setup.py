"""Seed the database with normalized 2026 Enterprise Edition demo data."""

import sys
import os
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import (
    Base, User, Patient, Doctor, Department, DoctorSchedule, Appointment,
    Billing, MedicalRecord, AppointmentStatus, BillingStatus, UserRole,
)
from database.connection import get_db, engine
from database.encrypt import encrypt_value, decrypt_value


def seed_database(session):
    """Seed the database with normalized demo data (for backward compatibility)."""
    # Skip if data already exists
    if session.query(Doctor).first():
        print("[SEED] Database already seeded — skipping.")
        return

    print("[SEED] Seeding database with normalized demo data...")
    try:
        # 1. Seed Departments
        dept_names = [
            ("Cardiology", "Heart and blood vessels care"),
            ("Neurology", "Brain and nervous system treatments"),
            ("Orthopedics", "Bones, joints, and muscular system"),
            ("Dental", "Oral health and cosmetic dentistry"),
            ("Dermatology", "Skin, hair, and nail treatments"),
            ("Pediatrics", "Infant, child, and adolescent medicine"),
            ("ENT", "Ear, nose, and throat care"),
            ("Oncology", "Cancer diagnosis and therapies")
        ]
        
        departments = {}
        for name, desc in dept_names:
            dept = Department(name=name, description=desc)
            session.add(dept)
            departments[name] = dept
        session.flush()

        # 2. Seed Doctors & User Credentials
        doctors_data = [
            ("Dr. Ananya Reddy", "MD, DM Cardiology (AIIMS)", 15, "Interventional Cardiology", "Cardiology", "English, Hindi, Telugu", 800, "Block A, Room 201", "Leading interventional cardiologist with 15 years of experience in complex angioplasties and heart failure management."),
            ("Dr. Rajesh Mehta", "MD, DM Neurology", 20, "Stroke & Epilepsy", "Neurology", "English, Hindi, Gujarati", 900, "Block A, Room 305", "Senior neurologist specializing in stroke management, epilepsy, and neurodegenerative disorders."),
            ("Dr. Suresh Nair", "MS Orthopaedics, Fellowship Sports Medicine", 12, "Joint Replacement & Sports Injuries", "Orthopedics", "English, Hindi, Malayalam", 700, "Block B, Room 102", "Expert orthopaedic surgeon specializing in knee/hip replacements and arthroscopic surgeries."),
            ("Dr. Kavitha Sundaram", "BDS, MDS Prosthodontics", 8, "Cosmetic Dentistry & Implants", "Dental", "English, Hindi, Tamil", 500, "Block C, Room 101", "Skilled dental surgeon known for cosmetic dentistry, dental implants, and smile makeovers."),
            ("Dr. Meera Iyer", "MD Dermatology, Fellowship Cosmetic Dermatology", 10, "Laser & Cosmetic Dermatology", "Dermatology", "English, Hindi, Kannada", 600, "Block C, Room 204", "Dermatologist with expertise in laser treatments, acne management, and anti-aging procedures."),
            ("Dr. Vikram Singh", "MD Paediatrics, Fellowship Neonatology", 14, "General Paediatrics & Neonatology", "Pediatrics", "English, Hindi, Punjabi", 650, "Block D, Room 101", "Compassionate paediatrician specializing in newborn care, childhood infections, and developmental milestones."),
            ("Dr. Fatima Khan", "MS ENT, Fellowship Head & Neck Surgery", 11, "Sinus Surgery & Hearing Disorders", "ENT", "English, Hindi, Urdu", 700, "Block B, Room 205", "ENT specialist experienced in endoscopic sinus surgery, cochlear implants, and voice disorders."),
            ("Dr. Arjun Desai", "MD, DM Medical Oncology", 18, "Chemotherapy & Targeted Therapy", "Oncology", "English, Hindi, Marathi", 1000, "Block A, Room 401", "Senior oncologist with 18 years of experience in managing solid tumours with chemotherapy and immunotherapy.")
        ]

        doctors = []
        for idx, (name, qual, exp, spec, dept_name, langs, fee, loc, bio) in enumerate(doctors_data, 1):
            username = f"doctor_{idx}"
            user = User(username=username, role=UserRole.DOCTOR, password_hash="pbkdf2:sha256:mock_hash")
            session.add(user)
            session.flush()

            doc = Doctor(
                doctor_uid=f"doc-{idx}",
                user_id=user.id,
                name=name,
                qualification=qual,
                experience_years=exp,
                specialization=spec,
                department_id=departments[dept_name].id,
                languages=langs,
                consultation_fee=fee,
                location=loc,
                bio=bio
            )
            session.add(doc)
            doctors.append(doc)
        session.flush()

        # 3. Seed Doctor Schedules (0=Mon … 4=Fri)
        schedules = []
        # Dr. Ananya Reddy – Mon/Wed/Fri 09:00-16:00
        for day in [0, 2, 4]:
            schedules.append(DoctorSchedule(doctor_id=doctors[0].id, day_of_week=day, start_time="09:00", end_time="16:00", slot_duration_mins=30))
        # Dr. Rajesh Mehta – Mon-Fri 10:00-17:00
        for day in range(5):
            schedules.append(DoctorSchedule(doctor_id=doctors[1].id, day_of_week=day, start_time="10:00", end_time="17:00", slot_duration_mins=30))
        # Dr. Suresh Nair – Tue/Thu/Sat 09:00-14:00
        for day in [1, 3, 5]:
            schedules.append(DoctorSchedule(doctor_id=doctors[2].id, day_of_week=day, start_time="09:00", end_time="14:00", slot_duration_mins=30))
        # Dr. Kavitha Sundaram – Mon-Fri 10:00-18:00
        for day in range(5):
            schedules.append(DoctorSchedule(doctor_id=doctors[3].id, day_of_week=day, start_time="10:00", end_time="18:00", slot_duration_mins=30))
        # Dr. Meera Iyer – Mon/Wed/Fri 11:00-17:00
        for day in [0, 2, 4]:
            schedules.append(DoctorSchedule(doctor_id=doctors[4].id, day_of_week=day, start_time="11:00", end_time="17:00", slot_duration_mins=30))
        # Dr. Vikram Singh – Mon-Sat 09:00-15:00
        for day in range(6):
            schedules.append(DoctorSchedule(doctor_id=doctors[5].id, day_of_week=day, start_time="09:00", end_time="15:00", slot_duration_mins=30))
        # Dr. Fatima Khan – Tue/Thu 10:00-16:00
        for day in [1, 3]:
            schedules.append(DoctorSchedule(doctor_id=doctors[6].id, day_of_week=day, start_time="10:00", end_time="16:00", slot_duration_mins=30))
        # Dr. Arjun Desai – Mon/Wed/Fri 09:00-13:00
        for day in [0, 2, 4]:
            schedules.append(DoctorSchedule(doctor_id=doctors[7].id, day_of_week=day, start_time="09:00", end_time="13:00", slot_duration_mins=45))
        session.add_all(schedules)
        session.flush()

        # 4. Seed Patients
        patients_data = [
            ("pat-rahul-uuid", "Rahul Sharma", "1991-07-12", 35, "Male", "+91-9876543210", "rahul.sharma@email.com", "12, Jubilee Hills, Hyderabad", "B+", "+91-9876543211"),
            ("pat-priya-uuid", "Priya Patel", "1998-02-28", 28, "Female", "+91-9123456789", "priya.patel@email.com", "45, Banjara Hills, Hyderabad", "A+", "+91-9123456790"),
            ("pat-amit-uuid", "Amit Kumar", "1974-11-05", 52, "Male", "+91-8765432109", "amit.kumar@email.com", "78, Madhapur, Hyderabad", "O+", "+91-8765432110"),
            ("pat-neha-uuid", "Neha Gupta", "1985-09-18", 41, "Female", "+91-7654321098", "neha.gupta@email.com", "23, Gachibowli, Hyderabad", "AB+", "+91-7654321099"),
            ("pat-sanjay-uuid", "Sanjay Verma", "1966-03-30", 60, "Male", "+91-6543210987", "sanjay.verma@email.com", "56, Hitech City, Hyderabad", "O-", "+91-6543210988")
        ]

        patients = []
        for p_uid, name, dob, age, gender, phone, email, address, bg, ec in patients_data:
            user = User(username=p_uid, role=UserRole.PATIENT, password_hash="pbkdf2:sha256:mock_hash")
            session.add(user)
            session.flush()

            pat = Patient(
                id=p_uid,
                user_id=user.id,
                name=name,
                dob=encrypt_value(dob),
                age=age,
                gender=gender,
                phone=encrypt_value(phone),
                email=encrypt_value(email),
                address=encrypt_value(address),
                blood_group=encrypt_value(bg),
                emergency_contact=encrypt_value(ec)
            )
            session.add(pat)
            patients.append(pat)
        session.flush()

        # 5. Seed Appointments
        today = date.today()
        appointments = [
            Appointment(appointment_uid="appt-1", patient_id="pat-rahul-uuid", doctor_id=doctors[0].id, department="Cardiology",
                        date=today + timedelta(days=3), time="09:30", status=AppointmentStatus.BOOKED, reason="Routine cardiac checkup"),
            Appointment(appointment_uid="appt-2", patient_id="pat-rahul-uuid", doctor_id=doctors[0].id, department="Cardiology",
                        date=today - timedelta(days=30), time="10:00", status=AppointmentStatus.COMPLETED, reason="Chest pain evaluation"),
            Appointment(appointment_uid="appt-3", patient_id="pat-priya-uuid", doctor_id=doctors[3].id, department="Dental",
                        date=today + timedelta(days=5), time="11:00", status=AppointmentStatus.BOOKED, reason="Dental cleaning"),
            Appointment(appointment_uid="appt-4", patient_id="pat-amit-uuid", doctor_id=doctors[1].id, department="Neurology",
                        date=today + timedelta(days=2), time="10:30", status=AppointmentStatus.BOOKED, reason="Migraine follow-up"),
            Appointment(appointment_uid="appt-5", patient_id="pat-amit-uuid", doctor_id=doctors[2].id, department="Orthopedics",
                        date=today - timedelta(days=5), time="09:00", status=AppointmentStatus.CANCELLED, reason="Knee pain consultation"),
            Appointment(appointment_uid="appt-6", patient_id="pat-neha-uuid", doctor_id=doctors[4].id, department="Dermatology",
                        date=today - timedelta(days=14), time="11:30", status=AppointmentStatus.COMPLETED, reason="Acne treatment follow-up"),
            Appointment(appointment_uid="appt-7", patient_id="pat-sanjay-uuid", doctor_id=doctors[7].id, department="Oncology",
                        date=today + timedelta(days=7), time="09:00", status=AppointmentStatus.BOOKED, reason="Chemotherapy cycle 3 review")
        ]
        session.add_all(appointments)
        session.flush()

        # 6. Seed Billing
        bills = [
            Billing(bill_number="BILL-2026-001", patient_id="pat-rahul-uuid", appointment_id=appointments[1].id,
                    amount=2500, paid=2500, pending=0, insurance_covered=1500, discount=0, payment_method="UPI", status=BillingStatus.PAID,
                    description="Cardiology consultation + ECG + Blood work"),
            Billing(bill_number="BILL-2026-002", patient_id="pat-rahul-uuid", appointment_id=appointments[0].id,
                    amount=800, paid=0, pending=800, insurance_covered=0, discount=0, payment_method=None, status=BillingStatus.PENDING,
                    description="Upcoming cardiology consultation fee"),
            Billing(bill_number="BILL-2026-003", patient_id="pat-priya-uuid", appointment_id=appointments[2].id,
                    amount=1500, paid=0, pending=1500, insurance_covered=0, discount=200, payment_method=None, status=BillingStatus.PENDING,
                    description="Dental cleaning + X-ray"),
            Billing(bill_number="BILL-2026-004", patient_id="pat-amit-uuid", appointment_id=appointments[3].id,
                    amount=3500, paid=1000, pending=2500, insurance_covered=1000, discount=0, payment_method="Credit Card", status=BillingStatus.PARTIAL,
                    description="Neurology consultation + MRI brain"),
            Billing(bill_number="BILL-2026-005", patient_id="pat-neha-uuid", appointment_id=appointments[5].id,
                    amount=1800, paid=1800, pending=0, insurance_covered=1200, discount=100, payment_method="Net Banking", status=BillingStatus.PAID,
                    description="Dermatology consultation + Laser session"),
            Billing(bill_number="BILL-2026-006", patient_id="pat-sanjay-uuid", appointment_id=appointments[6].id,
                    amount=25000, paid=10000, pending=15000, insurance_covered=15000, discount=0, payment_method="Insurance + UPI", status=BillingStatus.PARTIAL,
                    description="Oncology consultation + Chemotherapy cycle 3")
        ]
        session.add_all(bills)
        session.flush()

        # 7. Seed Medical Records
        records = [
            MedicalRecord(patient_id="pat-rahul-uuid", record_type="visit", date=today - timedelta(days=30), title="Cardiology Consultation", doctor_id=doctors[0].id,
                          details=encrypt_value("Patient presented with intermittent chest pain. ECG normal. Stress test advised. BP: 130/85. Prescribed Amlodipine 5mg.")),
            MedicalRecord(patient_id="pat-rahul-uuid", record_type="lab", date=today - timedelta(days=28), title="Complete Blood Count & Lipid Profile",
                          details=encrypt_value("Hb: 14.2 g/dL, WBC: 7500, Platelets: 2.5L. Total Cholesterol: 210 mg/dL (borderline high), LDL: 140, HDL: 45, Triglycerides: 180.")),
            MedicalRecord(patient_id="pat-rahul-uuid", record_type="prescription", date=today - timedelta(days=30), title="Prescription - Cardiac Medication", doctor_id=doctors[0].id,
                          details=encrypt_value("1. Amlodipine 5mg - once daily morning\n2. Atorvastatin 10mg - once daily night\n3. Ecosprin 75mg - once daily after lunch\nFollow-up in 1 month.")),
            MedicalRecord(patient_id="pat-priya-uuid", record_type="visit", date=today - timedelta(days=90), title="General Health Checkup",
                          details=encrypt_value("Annual health checkup. All vitals normal. BMI: 22.5. No significant findings. Advised regular exercise and balanced diet.")),
            MedicalRecord(patient_id="pat-priya-uuid", record_type="lab", date=today - timedelta(days=90), title="Annual Blood Work",
                          details=encrypt_value("Hb: 12.8 g/dL, FBS: 92 mg/dL, HbA1c: 5.2%, TSH: 2.8 mIU/L, Vitamin D: 28 ng/mL (slightly low). Vitamin D supplementation advised.")),
            MedicalRecord(patient_id="pat-amit-uuid", record_type="visit", date=today - timedelta(days=60), title="Neurology Consultation - Migraine", doctor_id=doctors[1].id,
                          details=encrypt_value("Chronic migraine with aura, 3-4 episodes/month. MRI brain ordered. Prescribed Sumatriptan 50mg as needed, Topiramate 25mg daily for prophylaxis.")),
            MedicalRecord(patient_id="pat-amit-uuid", record_type="imaging", date=today - timedelta(days=55), title="MRI Brain - Plain & Contrast",
                          details=encrypt_value("MRI findings: No acute intracranial pathology. No mass lesion or midline shift. Mild small vessel ischaemic changes. No aneurysm. Impression: Age-appropriate changes, no significant abnormality.")),
            MedicalRecord(patient_id="pat-neha-uuid", record_type="visit", date=today - timedelta(days=14), title="Dermatology Follow-up - Acne", doctor_id=doctors[4].id,
                          details=encrypt_value("Acne vulgaris improving with treatment. Reduced inflammatory lesions by 60%. Continuing isotretinoin 20mg. Skin hydration good. Next review in 4 weeks.")),
            MedicalRecord(patient_id="pat-sanjay-uuid", record_type="visit", date=today - timedelta(days=21), title="Oncology Review - Chemotherapy Cycle 2", doctor_id=doctors[7].id,
                          details=encrypt_value("Colon cancer Stage IIIA. Completed cycle 2 of FOLFOX regimen. Tolerated well. Mild nausea managed with ondansetron. WBC slightly low (3800). Next cycle in 3 weeks.")),
            MedicalRecord(patient_id="pat-sanjay-uuid", record_type="lab", date=today - timedelta(days=20), title="Post-Chemo Blood Work",
                          details=encrypt_value("Hb: 11.5 g/dL, WBC: 3800 (mildly low), Platelets: 1.8L, Neutrophils: 55%. Liver function: normal. Kidney function: normal. CEA: 8.2 (down from 15.6). Tumour markers trending down."))
        ]
        session.add_all(records)
        session.commit()
        print(f"[SEED] Seeded: {len(dept_names)} departments, {len(doctors)} doctors, {len(patients)} patients, "
              f"{len(appointments)} appointments, {len(bills)} bills, {len(records)} medical records.")

    except Exception as e:
        session.rollback()
        print(f"[SEED ERROR] Seeding failed: {e}")
        raise e


def reset_and_seed_database():
    """Drops all tables, recreates them, and seeds fresh normalized demo data."""
    print("[SEED] Resetting database tables...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("[SEED] Database tables created successfully.")

    session = get_db()
    try:
        seed_database(session)
    finally:
        session.close()


if __name__ == "__main__":
    reset_and_seed_database()

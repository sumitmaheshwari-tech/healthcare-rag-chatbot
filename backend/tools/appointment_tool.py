"""Appointment management tools — check availability, book, cancel, list."""

import os
import sys
import uuid
from datetime import datetime, date, timedelta
from typing import Annotated
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_db
from database.models import (
    Doctor, DoctorSchedule, Appointment, AppointmentStatus,
    Billing, BillingStatus, DoctorLeave, DoctorHoliday,
    Patient, Invoice, Notification
)
from database.encrypt import decrypt_value
from database.audit import log_audit_event


def _normalize_time(raw_time: str) -> str:
    """Normalize a time string from any common format to 24h HH:MM.
    
    Handles: '10:00 AM', '2:30 pm', '12 AM', '12 PM', '14:00', '9am', '09:00', etc.
    Returns the normalised 'HH:MM' string, or the original input if parsing fails.
    """
    import re as _re
    t = raw_time.strip()
    if not t:
        return t

    # Try common 12h formats: '10:00 AM', '2:30pm', '12 AM', '9am'
    m = _re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM|a\.m\.|p\.m\.)$', t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        period = m.group(3).lower().replace('.', '')
        if period == 'am':
            if hour == 12:
                hour = 0
        elif period == 'pm':
            if hour != 12:
                hour += 12
        return f"{hour:02d}:{minute:02d}"

    # Try 24h format: '14:00', '09:30'
    m2 = _re.match(r'^(\d{1,2}):(\d{2})$', t)
    if m2:
        return f"{int(m2.group(1)):02d}:{m2.group(2)}"

    return t


def _generate_slots(start: str, end: str, duration: int) -> list[str]:
    """Generate time-slot strings between *start* and *end* at *duration*-min intervals."""
    fmt = "%H:%M"
    t = datetime.strptime(start, fmt)
    t_end = datetime.strptime(end, fmt)
    slots = []
    while t < t_end:
        slots.append(t.strftime(fmt))
        t += timedelta(minutes=duration)
    return slots


@tool
def check_doctor_availability(doctor_name: str, date: str = "", date_str: str = "") -> str:
    """Check available appointment slots for a specific doctor or department on a given date.

    Args:
        doctor_name: Full or partial name of the doctor (e.g. 'Dr. Ananya Reddy') or department ('Cardiology').
        date: The date to check in YYYY-MM-DD format (e.g. '2026-09-05').
        date_str: The date to check in YYYY-MM-DD format (e.g. '2026-09-05').
    """
    db = get_db()
    try:
        target_date_raw = date or date_str
        if not target_date_raw:
            return "Please provide a date in YYYY-MM-DD format (e.g. 2026-09-05)."

        # Find doctor by Name OR by Department
        doctor = (
            db.query(Doctor)
            .filter(Doctor.name.ilike(f"%{doctor_name}%"))
            .first()
        )
        if not doctor:
            # Try searching by specialization or department
            from database.models import Department
            dept = db.query(Department).filter(Department.name.ilike(f"%{doctor_name}%")).first()
            if dept:
                docs = db.query(Doctor).filter(Doctor.department_id == dept.id).all()
                if docs:
                    doctor = docs[0]
            else:
                doctor = db.query(Doctor).filter(Doctor.specialization.ilike(f"%{doctor_name}%")).first()

        if not doctor:
            return f"Sorry, I could not find a doctor or department matching '{doctor_name}'. Please check the specialist name or department."

        # Parse date
        try:
            target_date = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            return "Invalid date format. Please use YYYY-MM-DD (e.g. 2026-09-05)."

        if target_date < datetime.now().date():
            return "That date is in the past. Please choose today or a future date."

        # Check hospital holiday
        holiday = db.query(DoctorHoliday).filter(DoctorHoliday.date == target_date).first()
        if holiday:
            return f"Sorry, the hospital is closed on {target_date.strftime('%A, %B %d, %Y')} due to a holiday: '{holiday.name}'."

        # Check doctor leave
        leave = db.query(DoctorLeave).filter(
            DoctorLeave.doctor_id == doctor.id,
            DoctorLeave.date == target_date
        ).first()
        if leave:
            return f"Sorry, {doctor.name} is on leave on {target_date.strftime('%A, %B %d, %Y')} (Reason: {leave.reason or 'Out of Office'})."

        day_of_week = target_date.weekday()  # 0=Mon … 6=Sun

        # Get schedule for that day
        schedule = (
            db.query(DoctorSchedule)
            .filter(
                DoctorSchedule.doctor_id == doctor.id,
                DoctorSchedule.day_of_week == day_of_week,
                DoctorSchedule.is_active == True,
            )
            .first()
        )
        if not schedule:
            return (
                f"{doctor.name} ({doctor.department.name if doctor.department else 'Specialist'}) "
                f"does not have clinic hours on {target_date.strftime('%A')}s. "
                f"Please try another day of the week."
            )

        # All possible slots for doctor
        all_slots = _generate_slots(
            schedule.start_time, schedule.end_time, schedule.slot_duration_mins
        )

        # Already-booked slots
        booked = (
            db.query(Appointment.time)
            .filter(
                Appointment.doctor_id == doctor.id,
                Appointment.date == target_date,
                Appointment.status == AppointmentStatus.BOOKED,
            )
            .all()
        )
        booked_times = {row.time for row in booked}
        open_slots = [s for s in all_slots if s not in booked_times]

        if not open_slots:
            # Find next available date with free slots
            next_date = target_date + timedelta(days=1)
            found_alt = None
            for _ in range(10):
                dow = next_date.weekday()
                sch = db.query(DoctorSchedule).filter(
                    DoctorSchedule.doctor_id == doctor.id,
                    DoctorSchedule.day_of_week == dow,
                    DoctorSchedule.is_active == True
                ).first()
                if sch:
                    b_set = {
                        r.time for r in db.query(Appointment.time).filter(
                            Appointment.doctor_id == doctor.id,
                            Appointment.date == next_date,
                            Appointment.status == AppointmentStatus.BOOKED
                        ).all()
                    }
                    d_slots = _generate_slots(sch.start_time, sch.end_time, sch.slot_duration_mins)
                    free = [s for s in d_slots if s not in b_set]
                    if free:
                        found_alt = (next_date, free[:5])
                        break
                next_date += timedelta(days=1)

            if found_alt:
                alt_d, alt_s = found_alt
                friendly_s = [datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0") for s in alt_s]
                return (
                    f"⚠️ **Fully Booked:** {doctor.name} is fully booked on {target_date.strftime('%A, %B %d, %Y')}.

"
                    f"💡 **Next Available Date:** **{alt_d.strftime('%A, %B %d, %Y')}**
"
                    f"• Available Slots: {', '.join(friendly_s)}

"
                    f"Would you like to schedule an appointment on this date instead?"
                )
            else:
                return f"Sorry, {doctor.name} is fully booked on {target_date.strftime('%A, %B %d, %Y')}. Please choose another date."

        # Classify slots into Morning, Afternoon, and Evening suggestions
        morning_slots = []
        afternoon_slots = []
        evening_slots = []

        for s in open_slots:
            try:
                dt = datetime.strptime(s, "%H:%M")
                display_time = dt.strftime("%I:%M %p").lstrip("0")
                h = dt.hour
                if h < 12:
                    morning_slots.append(display_time)
                elif h < 16:
                    afternoon_slots.append(display_time)
                else:
                    evening_slots.append(display_time)
            except Exception:
                morning_slots.append(s)

        res_str = (
            f"📅 **Available Slots for {doctor.name}** "
            f"({doctor.department.name if doctor.department else 'Specialist'}, {doctor.specialization})
"
            f"📆 **Date:** {target_date.strftime('%A, %B %d, %Y')}

"
        )
        if morning_slots:
            res_str += f"🌅 **Morning:** {', '.join(morning_slots)}
"
        if afternoon_slots:
            res_str += f"☀️ **Afternoon:** {', '.join(afternoon_slots)}
"
        if evening_slots:
            res_str += f"🌆 **Evening:** {', '.join(evening_slots)}
"

        res_str += (
            f"
• **Consultation Fee:** ₹{doctor.consultation_fee:.0f}
"
            f"• **Location:** {doctor.location}

"
            f"Please let me know which time slot you would like to book!"
        )
        return res_str
    except Exception as e:
        return f"Error checking availability: {e}"
    finally:
        db.close()


@tool
def book_appointment(
    patient_id: str,
    doctor_name: str,
    state: Annotated[dict, InjectedState],
    date: str = "",
    date_str: str = "",
    time: str = "",
    time_str: str = "",
    reason: str = "General Consultation",
) -> str:
    """Book an appointment for a patient with a specific doctor or department.

    Args:
        patient_id: The patient's string UID (e.g. 'pat-a95bc5aa5d29').
        doctor_name: Full or partial name of the doctor (e.g. 'Dr. Rajesh Mehta') or department.
        date: Appointment date in YYYY-MM-DD format (e.g. '2026-09-05').
        date_str: Appointment date in YYYY-MM-DD format.
        time: Appointment time in HH:MM or 12h format (e.g. '10:00 AM' or '10:00').
        time_str: Appointment time in HH:MM or 12h format.
        reason: Reason for the visit (default: 'General Consultation').
    """
    target_date_raw = date or date_str
    target_time_raw = time or time_str
    if not target_date_raw or not target_time_raw:
        return "Please provide both a date (YYYY-MM-DD) and a time slot (e.g. 10:00 AM) to complete the booking."

    target_time_normalized = _normalize_time(target_time_raw)

    auth_uid = state.get("authenticated_patient_uid") if state else None
    if not auth_uid or auth_uid != patient_id:
        log_audit_event(
            request_id="tool-call",
            action="BOOK_APPOINTMENT",
            status="FAILED",
            patient_uid=patient_id,
            details=f"BOLA mismatch: authenticated as {auth_uid}"
        )
        return "I'm sorry, but you must be signed in to book an appointment."

    db = get_db()
    try:
        # 1. Find doctor by Name OR Department
        doctor = db.query(Doctor).filter(Doctor.name.ilike(f"%{doctor_name}%")).first()
        if not doctor:
            from database.models import Department
            dept = db.query(Department).filter(Department.name.ilike(f"%{doctor_name}%")).first()
            if dept:
                docs = db.query(Doctor).filter(Doctor.department_id == dept.id).all()
                if docs:
                    doctor = docs[0]
            else:
                doctor = db.query(Doctor).filter(Doctor.specialization.ilike(f"%{doctor_name}%")).first()

        if not doctor:
            return f"Doctor or department '{doctor_name}' not found. Please specify a doctor like Dr. Rajesh Mehta or a department like Cardiology."

        try:
            target_date = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            return "Invalid date format. Please use YYYY-MM-DD (e.g. 2026-09-05)."

        if target_date < datetime.now().date():
            return "Cannot book an appointment in the past. Please select today or a future date."

        # Check hospital holiday
        holiday = db.query(DoctorHoliday).filter(DoctorHoliday.date == target_date).first()
        if holiday:
            return f"Failed to book: The hospital is closed on {target_date.strftime('%B %d, %Y')} due to: '{holiday.name}'."

        # Check doctor leave
        leave = db.query(DoctorLeave).filter(
            DoctorLeave.doctor_id == doctor.id,
            DoctorLeave.date == target_date
        ).first()
        if leave:
            return f"Failed to book: {doctor.name} is on leave on {target_date.strftime('%B %d, %Y')} ({leave.reason or 'Out of Office'})."

        # Check doctor schedule day
        day_of_week = target_date.weekday()
        schedule = db.query(DoctorSchedule).filter(
            DoctorSchedule.doctor_id == doctor.id,
            DoctorSchedule.day_of_week == day_of_week,
            DoctorSchedule.is_active == True
        ).first()
        if not schedule:
            return f"{doctor.name} does not have clinic hours on {target_date.strftime('%A')}s. Please choose another day."

        all_slots = _generate_slots(schedule.start_time, schedule.end_time, schedule.slot_duration_mins)
        if target_time_normalized not in all_slots:
            hour_match = [s for s in all_slots if s.startswith(target_time_normalized.split(':')[0] + ':')]
            if len(hour_match) == 1:
                target_time_normalized = hour_match[0]
            else:
                display_slots = []
                for s in all_slots:
                    try:
                        dt = datetime.strptime(s, '%H:%M')
                        display_slots.append(dt.strftime('%I:%M %p').lstrip('0'))
                    except Exception:
                        display_slots.append(s)
                return f"The time '{target_time_raw}' is not a valid clinic slot. Valid slots on this day are: {', '.join(display_slots)}"

        # ── 2. Check if the slot is ALREADY TAKEN BY ANOTHER PATIENT ──
        existing = (
            db.query(Appointment)
            .filter(
                Appointment.doctor_id == doctor.id,
                Appointment.date == target_date,
                Appointment.time == target_time_normalized,
                Appointment.status == AppointmentStatus.BOOKED,
            )
            .first()
        )
        if existing:
            # Query all remaining free slots on this date
            booked_times = {
                row.time for row in db.query(Appointment.time).filter(
                    Appointment.doctor_id == doctor.id,
                    Appointment.date == target_date,
                    Appointment.status == AppointmentStatus.BOOKED,
                ).all()
            }
            open_slots = [s for s in all_slots if s not in booked_times]

            if open_slots:
                formatted_free = []
                for s in open_slots:
                    try:
                        dt = datetime.strptime(s, "%H:%M")
                        formatted_free.append(dt.strftime("%I:%M %p").lstrip("0"))
                    except Exception:
                        formatted_free.append(s)

                return (
                    f"⚠️ **Slot Unavailable:** The `{target_time_raw}` slot with **{doctor.name}** "
                    f"({doctor.department.name if doctor.department else 'General'}) on "
                    f"**{target_date.strftime('%A, %B %d, %Y')}** is already booked by another patient.

"
                    f"💡 **Available Alternative Slots for {doctor.name} on this day:**
"
                    f"• {', '.join(formatted_free)}

"
                    f"Would you like to book one of these available time slots instead?"
                )
            else:
                # Find next available date with free slots
                next_date = target_date + timedelta(days=1)
                found_alt = None
                for _ in range(7):
                    dow = next_date.weekday()
                    sch = db.query(DoctorSchedule).filter(
                        DoctorSchedule.doctor_id == doctor.id,
                        DoctorSchedule.day_of_week == dow,
                        DoctorSchedule.is_active == True
                    ).first()
                    if sch:
                        day_booked = {
                            row.time for row in db.query(Appointment.time).filter(
                                Appointment.doctor_id == doctor.id,
                                Appointment.date == next_date,
                                Appointment.status == AppointmentStatus.BOOKED,
                            ).all()
                        }
                        day_slots = _generate_slots(sch.start_time, sch.end_time, sch.slot_duration_mins)
                        day_open = [s for s in day_slots if s not in day_booked]
                        if day_open:
                            found_alt = (next_date, day_open[:5])
                            break
                    next_date += timedelta(days=1)

                if found_alt:
                    alt_d, alt_s = found_alt
                    formatted_alt = [datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0") for s in alt_s]
                    return (
                        f"⚠️ **Fully Booked:** {doctor.name} is fully booked on {target_date.strftime('%A, %B %d, %Y')}.

"
                        f"💡 **Next Available Date:** **{alt_d.strftime('%A, %B %d, %Y')}**
"
                        f"• Open Slots: {', '.join(formatted_alt)}

"
                        f"Would you like to book an appointment on {alt_d.strftime('%B %d')} instead?"
                    )
                else:
                    return f"Sorry, {doctor.name} has no available slots on {target_date.strftime('%A, %B %d, %Y')}. Please choose another date."

        # ── 3. Check Patient double-booking at the same time ──
        patient_overlap = (
            db.query(Appointment)
            .filter(
                Appointment.patient_id == patient_id,
                Appointment.date == target_date,
                Appointment.time == target_time_normalized,
                Appointment.status == AppointmentStatus.BOOKED
            )
            .first()
        )
        if patient_overlap:
            return f"You already have an appointment booked on {target_date.strftime('%B %d, %Y')} at {target_time_raw} (UID: {patient_overlap.appointment_uid})."

        # ── 4. Retrieve patient details and Create appointment ──
        patient = db.query(Patient).filter(Patient.id == patient_id).first()
        patient_name = patient.name if patient else "Patient"
        patient_phone = decrypt_value(patient.phone) if (patient and patient.phone) else "N/A"

        appt = Appointment(
            appointment_uid=f"appt-{uuid.uuid4().hex[:8]}",
            patient_id=patient_id,
            doctor_id=doctor.id,
            department=doctor.department.name if doctor.department else "General",
            date=target_date,
            time=target_time_normalized,
            status=AppointmentStatus.BOOKED,
            reason=reason,
            notes=(
                f"Booked via MedCare Chatbot.
"
                f"Patient Name: {patient_name}
"
                f"Phone: {patient_phone}
"
                f"Patient UID: {patient_id}
"
                f"Doctor: {doctor.name} ({doctor.specialization})
"
                f"Location: {doctor.location}
"
                f"Fee: ₹{doctor.consultation_fee:.2f}"
            )
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        bill_no = f"INV-{appt.id}-{int(datetime.now().timestamp())}"

        bill = Billing(
            bill_number=bill_no,
            patient_id=patient_id,
            appointment_id=appt.id,
            amount=doctor.consultation_fee,
            paid=0.0,
            pending=doctor.consultation_fee,
            payment_method="Unpaid",
            status=BillingStatus.PENDING,
            description=(
                f"Consultation Appointment #{appt.id} with {doctor.name} | "
                f"Patient: {patient_name} ({patient_id}) | Phone: {patient_phone}"
            )
        )
        db.add(bill)
        db.commit()
        db.refresh(bill)

        invoice = Invoice(
            invoice_number=bill_no,
            billing_id=bill.id,
            amount=doctor.consultation_fee,
            status="issued"
        )
        db.add(invoice)

        notif = Notification(
            patient_id=patient_id,
            message=(
                f"Appointment #{appt.id} confirmed with {doctor.name} for "
                f"{target_date.strftime('%A, %B %d, %Y')} at {time_str or target_time_normalized}. "
                f"Invoice #{bill_no} generated."
            )
        )
        db.add(notif)
        db.commit()

        log_audit_event(
            request_id="tool-call",
            action="BOOK_APPOINTMENT",
            status="SUCCESS",
            patient_uid=patient_id,
            resource=f"appointments/{appt.appointment_uid}",
            details=f"Appointment {appt.appointment_uid} booked with {doctor.name} on {target_date} at {target_time_normalized}."
        )

        # Send Telegram push alert to Admin
        try:
            from services.telegram_auth_service import send_telegram_booking_alert
            send_telegram_booking_alert({
                "appointment_uid": appt.appointment_uid,
                "patient_name": patient_name,
                "patient_phone": patient_phone,
                "doctor_name": doctor.name,
                "department": doctor.department.name if doctor.department else "General",
                "date": target_date.strftime('%A, %B %d, %Y'),
                "time": time_str or target_time_normalized,
                "reason": reason,
                "fee": doctor.consultation_fee,
                "invoice_number": bill_no
            })
        except Exception as tg_err:
            print(f"[TELEGRAM ALERT ERROR] Failed to send admin alert: {tg_err}")

        time_display = target_time_raw
        try:
            time_display = datetime.strptime(target_time_normalized, "%H:%M").strftime("%I:%M %p").lstrip("0")
        except Exception:
            pass

        return (
            f"✅ **Appointment Confirmed Successfully!**

"
            f"• **Appointment ID:** `{appt.appointment_uid}`
"
            f"• **Patient Name:** {patient_name}
"
            f"• **Doctor:** {doctor.name} ({doctor.specialization})
"
            f"• **Department:** {doctor.department.name if doctor.department else 'General'}
"
            f"• **Date:** {target_date.strftime('%A, %B %d, %Y')}
"
            f"• **Time:** **{time_display}**
"
            f"• **Location:** {doctor.location}
"
            f"• **Consultation Fee:** ₹{doctor.consultation_fee:.0f}
"
            f"• **Invoice Number:** `{bill_no}`

"
            f"An SMS & Telegram confirmation has been sent to your registered number."
        )
    except Exception as e:
        db.rollback()
        log_audit_event(
            request_id="tool-call",
            action="BOOK_APPOINTMENT",
            status="ERROR",
            patient_uid=patient_id,
            details=f"Database error during booking: {e}"
        )
        return f"Error booking appointment: {e}"
    finally:
        db.close()


@tool
def cancel_appointment(appointment_id: int, state: Annotated[dict, InjectedState]) -> str:
    """Cancel an existing appointment by its appointment ID.

    Args:
        appointment_id: The numeric ID of the appointment to cancel.

    Use this when the user wants to cancel a specific appointment.
    """
    db = get_db()
    try:
        appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appt:
            return f"Appointment #{appointment_id} not found."

        # Enforce BOLA authorization check
        auth_uid = state.get("authenticated_patient_uid") if state else None
        if not auth_uid or auth_uid != appt.patient_id:
            log_audit_event(
                request_id="tool-call",
                action="CANCEL_APPOINTMENT",
                status="FAILED",
                patient_uid=appt.patient_id,
                details=f"Unauthorized cancel attempt for appt #{appointment_id} by authenticated user {auth_uid}"
            )
            return "I'm sorry, but you are not authorized to cancel this appointment."

        if appt.status == AppointmentStatus.CANCELLED:
            return f"Appointment #{appointment_id} is already cancelled."

        if appt.status == AppointmentStatus.COMPLETED:
            return f"Appointment #{appointment_id} has already been completed and cannot be cancelled."

        doctor = db.query(Doctor).filter(Doctor.id == appt.doctor_id).first()
        appt.status = AppointmentStatus.CANCELLED
        db.commit()

        log_audit_event(
            request_id="tool-call",
            action="CANCEL_APPOINTMENT",
            status="SUCCESS",
            patient_uid=appt.patient_id,
            resource=f"appointments/{appointment_id}"
        )

        return (
            f"✅ Appointment #{appointment_id} has been cancelled.\n\n"
            f"**Doctor:** {doctor.name if doctor else 'N/A'}\n"
            f"**Date:** {appt.date.strftime('%A, %B %d, %Y')}\n"
            f"**Time:** {appt.time}\n\n"
            f"If you'd like to rebook, just let me know!"
        )
    except Exception as e:
        db.rollback()
        log_audit_event(
            request_id="tool-call",
            action="CANCEL_APPOINTMENT",
            status="FAILED",
            details=f"Cancellation error: {e}"
        )
        return f"Error cancelling appointment: {e}"
    finally:
        db.close()


@tool
def get_patient_appointments(patient_id: str, state: Annotated[dict, InjectedState]) -> str:
    """Get all upcoming and recent appointments for a patient.

    Args:
        patient_id: The patient's string UID.

    Use this when the user wants to see their scheduled appointments or
    appointment history.
    """
    # Enforce BOLA authorization check
    auth_uid = state.get("authenticated_patient_uid") if state else None
    if not auth_uid or auth_uid != patient_id:
        log_audit_event(
            request_id="tool-call",
            action="VIEW_APPOINTMENTS",
            status="FAILED",
            patient_uid=patient_id,
            details=f"BOLA mismatch: authenticated as {auth_uid}"
        )
        return "I'm sorry, but you are not authorized to view appointments for this patient."

    db = get_db()
    try:
        appointments = (
            db.query(Appointment)
            .filter(Appointment.patient_id == patient_id)
            .order_by(Appointment.date.desc(), Appointment.time.desc())
            .all()
        )

        if not appointments:
            log_audit_event(
                request_id="tool-call",
                action="VIEW_APPOINTMENTS",
                status="SUCCESS",
                patient_uid=patient_id,
                details="No appointments found."
            )
            return "You have no appointments on record."

        upcoming = []
        past = []
        today = date.today()

        for appt in appointments:
            doctor = db.query(Doctor).filter(Doctor.id == appt.doctor_id).first()
            doc_name = doctor.name if doctor else "Unknown"
            line = (
                f"• **Appointment #{appt.id}** — {doc_name} ({appt.department})\n"
                f"  Date: {appt.date.strftime('%A, %B %d, %Y')} at {appt.time}\n"
                f"  Status: {appt.status.value.capitalize()}\n"
                f"  Reason: {appt.reason or 'N/A'}"
            )
            if appt.date >= today and appt.status == AppointmentStatus.BOOKED:
                upcoming.append(line)
            else:
                past.append(line)

        parts = []
        if upcoming:
            parts.append("**📅 Upcoming Appointments:**\n" + "\n\n".join(upcoming))
        if past:
            parts.append("**📋 Past / Other Appointments:**\n" + "\n\n".join(past[:5]))

        log_audit_event(
            request_id="tool-call",
            action="VIEW_APPOINTMENTS",
            status="SUCCESS",
            patient_uid=patient_id,
            resource=f"appointments/patient/{patient_id}"
        )

        if not upcoming and not past:
            return "You have no appointments on record."

        return "\n\n".join(parts)
    except Exception as e:
        return f"Error retrieving appointments: {e}"
    finally:
        db.close()

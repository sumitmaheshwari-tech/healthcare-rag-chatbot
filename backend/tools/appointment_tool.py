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
    """Check available appointment slots for a specific doctor on a given date.

    Args:
        doctor_name: Full or partial name of the doctor (e.g. 'Dr. Ananya Reddy' or 'Ananya').
        date: The date to check in YYYY-MM-DD format (e.g. '2026-08-10').
        date_str: The date to check in YYYY-MM-DD format (e.g. '2026-08-10').
    """
    db = get_db()
    try:
        target_date_raw = date or date_str
        if not target_date_raw:
            return "Please provide a date in YYYY-MM-DD format (e.g. 2026-08-10)."

        # Find doctor
        doctor = (
            db.query(Doctor)
            .filter(Doctor.name.ilike(f"%{doctor_name}%"))
            .first()
        )
        if not doctor:
            return f"Sorry, I could not find a doctor matching '{doctor_name}'. Please check the name and try again."

        # Parse date
        try:
            target_date = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            return "Invalid date format. Please use YYYY-MM-DD (e.g. 2026-07-15)."

        if target_date < datetime.now().date():
            return "That date is in the past. Please choose a future date."

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
                f"{doctor.name} does not have a scheduled clinic on "
                f"{target_date.strftime('%A, %B %d, %Y')}. "
                f"Please try a different date."
            )

        # All possible slots
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
            return (
                f"Sorry, {doctor.name} is fully booked on "
                f"{target_date.strftime('%A, %B %d, %Y')}. "
                f"Please try another date."
            )

        # Classify slots into Morning, Afternoon, and Evening suggestions
        morning_slots = []
        afternoon_slots = []
        evening_slots = []
        for s in open_slots:
            try:
                t_val = datetime.strptime(s, "%H:%M").time()
                if t_val < datetime.strptime("12:00", "%H:%M").time():
                    morning_slots.append(s)
                elif t_val < datetime.strptime("16:00", "%H:%M").time():
                    afternoon_slots.append(s)
                else:
                    evening_slots.append(s)
            except Exception:
                morning_slots.append(s)

        res_str = (
            f"📅 **Availability for {doctor.name} ({doctor.specialization}) on "
            f"{target_date.strftime('%A, %B %d, %Y')}:**\n\n"
        )
        if morning_slots:
            res_str += f"🌅 **Morning (09:00 - 12:00):** {', '.join(morning_slots)}\n"
        if afternoon_slots:
            res_str += f"☀️ **Afternoon (12:00 - 16:00):** {', '.join(afternoon_slots)}\n"
        if evening_slots:
            res_str += f"🌆 **Evening (16:00 - 19:00):** {', '.join(evening_slots)}\n"

        res_str += (
            f"\n• **Consultation Fee:** ₹{doctor.consultation_fee:.0f}\n"
            f"• **Location:** {doctor.location}"
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
    """Book an appointment for a patient with a specific doctor.

    Args:
        patient_id: The patient's string UID.
        doctor_name: Full or partial name of the doctor.
        date: Appointment date in YYYY-MM-DD format.
        date_str: Appointment date in YYYY-MM-DD format.
        time: Appointment time in HH:MM format (e.g. 10:00 AM or 10:00).
        time_str: Appointment time in HH:MM format.
        reason: Reason for the visit (default: 'General Consultation').
    """
    target_date_raw = date or date_str
    target_time_raw = time or time_str
    if not target_date_raw or not target_time_raw:
        return "Please provide both a date (YYYY-MM-DD) and a time slot (e.g. 10:00 AM) to complete the booking."

    # Normalize time to 24h HH:MM format (handles '10:00 AM', '12 AM', '2pm', etc.)
    target_time_normalized = _normalize_time(target_time_raw)

    # Enforce BOLA authorization check
    auth_uid = state.get("authenticated_patient_uid") if state else None
    if not auth_uid or auth_uid != patient_id:
        log_audit_event(
            request_id="tool-call",
            action="BOOK_APPOINTMENT",
            status="FAILED",
            patient_uid=patient_id,
            details=f"BOLA mismatch: authenticated as {auth_uid}"
        )
        return "I'm sorry, but you are not authorized to book an appointment for this patient."

    db = get_db()
    try:
        # 1. Find doctor
        doctor = db.query(Doctor).filter(Doctor.name.ilike(f"%{doctor_name}%")).first()
        if not doctor:
            return f"Doctor '{doctor_name}' not found."

        try:
            target_date = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            return "Invalid date format. Please use YYYY-MM-DD."

        if target_date < datetime.now().date():
            return "Cannot book an appointment in the past."

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
            return f"{doctor.name} does not work on {target_date.strftime('%A')}s."

        # Check slot is within working hours
        all_slots = _generate_slots(schedule.start_time, schedule.end_time, schedule.slot_duration_mins)
        if target_time_normalized not in all_slots:
            # Try a fuzzy hour-only match (e.g. user said '10' meaning '10:00')
            hour_match = [s for s in all_slots if s.startswith(target_time_normalized.split(':')[0] + ':')]
            if len(hour_match) == 1:
                target_time_normalized = hour_match[0]
            else:
                # Format available slots in 12h for user-friendly display
                display_slots = []
                for s in all_slots:
                    try:
                        dt = datetime.strptime(s, '%H:%M')
                        display_slots.append(dt.strftime('%I:%M %p').lstrip('0'))
                    except Exception:
                        display_slots.append(s)
                return f"The time '{target_time_raw}' is not a valid clinic slot. Available slots are: {', '.join(display_slots)}"

        # Check the slot is not already taken
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
            return (
                f"Sorry, the {time_str} slot with {doctor.name} on "
                f"{target_date.strftime('%B %d, %Y')} is already booked."
            )

        # Retrieve patient record to extract real-time details
        patient = db.query(Patient).filter(Patient.id == patient_id).first()
        patient_name = patient.name if patient else "Unknown"
        patient_phone = decrypt_value(patient.phone) if (patient and patient.phone) else "N/A"

        # Create appointment with rich notes
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
                f"Booked via MedCare Chatbot.\n"
                f"Patient Name: {patient_name}\n"
                f"Phone: {patient_phone}\n"
                f"Patient UUID: {patient_id}\n"
                f"Location: {doctor.location}\n"
                f"Fee: ₹{doctor.consultation_fee:.2f}"
            )
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        # Generate invoice number
        bill_no = f"INV-{appt.id}-{int(datetime.now().timestamp())}"

        # 1. Create billing record in billings table
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

        # 2. Create invoice record in invoices table
        invoice = Invoice(
            invoice_number=bill_no,
            billing_id=bill.id,
            amount=doctor.consultation_fee,
            status="issued"
        )
        db.add(invoice)

        # 3. Create notification record in notifications table
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

        # Trigger real-time Admin Telegram & Email alert (non-blocking in background)
        try:
            from utils.notifier import notify_admin_booking_completed
            notify_admin_booking_completed({
                "appointment_id": appt.id,
                "patient_name": patient_name,
                "patient_uid": patient_id,
                "doctor_name": doctor.name,
                "department": doctor.department.name if doctor.department else "General",
                "date": target_date.strftime('%A, %B %d, %Y'),
                "time_slot": time_str or target_time_normalized,
                "fee": f"{doctor.consultation_fee:.2f}",
                "invoice_number": bill_no,
                "location": doctor.location or f"Room {doctor.room_number}, {doctor.block}",
                "reason": reason or "General Consultation",
            })
        except Exception as ne:
            print(f"[NOTIFIER ERROR] Failed to dispatch admin alert: {ne}")

        log_audit_event(
            request_id="tool-call",
            action="BOOK_APPOINTMENT",
            status="SUCCESS",
            patient_uid=patient_id,
            resource=f"appointments/{appt.id}"
        )

        return (
            f"✅ **Appointment Booked Successfully!**\n\n"
            f"• **Appointment ID:** #{appt.id}\n"
            f"• **Doctor:** {doctor.name} ({doctor.specialization})\n"
            f"• **Date:** {target_date.strftime('%A, %B %d, %Y')}\n"
            f"• **Time:** {time_str}\n"
            f"• **Location:** {doctor.location}\n"
            f"• **Consultation Fee:** ₹{doctor.consultation_fee:.2f} (To be paid at desk)\n"
            f"• **Invoice:** {bill_no}\n\n"
            f"Please arrive 15 minutes early for check-in. Thank you!"
        )
    except Exception as e:
        db.rollback()
        log_audit_event(
            request_id="tool-call",
            action="BOOK_APPOINTMENT",
            status="FAILED",
            patient_uid=patient_id,
            details=f"Booking error: {e}"
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

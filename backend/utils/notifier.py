"""Admin notification module — sends real-time booking alerts via Telegram and Email."""
import os
import threading
import requests
from config import settings


def send_telegram_alert(booking_data: dict):
    """Send an instant HTML push notification to Admin Telegram."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_ADMIN_CHAT_ID

    if not token or not chat_id:
        return

    message = (
        "🚨 <b>NEW APPOINTMENT BOOKED</b> 🚨\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Patient:</b> {booking_data.get('patient_name', 'N/A')}\n"
        f"🆔 <b>Patient ID:</b> <code>{booking_data.get('patient_uid', 'N/A')}</code>\n"
        f"👨‍⚕️ <b>Doctor:</b> {booking_data.get('doctor_name', 'N/A')}\n"
        f"🏢 <b>Specialty:</b> {booking_data.get('department', 'N/A')}\n"
        f"📝 <b>Reason / Symptoms:</b> {booking_data.get('reason', 'General Consultation')}\n"
        f"📅 <b>Date:</b> <code>{booking_data.get('date', 'N/A')}</code>\n"
        f"⏰ <b>Time Slot:</b> <code>{booking_data.get('time_slot', 'N/A')}</code>\n"
        f"💵 <b>Consultation Fee:</b> ₹{booking_data.get('fee', 'N/A')}\n"
        f"🧾 <b>Invoice Number:</b> <code>{booking_data.get('invoice_number', 'N/A')}</code>\n"
        f"📍 <b>Location:</b> {booking_data.get('location', 'N/A')}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏥 <i>MedCare Hospital Management System</i>"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            print(f"[NOTIFIER] Telegram alert sent to chat_id {chat_id} for appointment #{booking_data.get('appointment_id', '')}")
        else:
            print(f"[NOTIFIER WARNING] Telegram API returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[NOTIFIER WARNING] Failed to send Telegram alert: {e}")


def send_email_alert(booking_data: dict):
    """Send an HTML email notification if SMTP is configured."""
    smtp_email = settings.SMTP_EMAIL
    smtp_password = settings.SMTP_PASSWORD
    admin_email = settings.ADMIN_EMAIL

    if not smtp_email or not smtp_password or not admin_email:
        return

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏥 New Booking: {booking_data.get('patient_name')} with {booking_data.get('doctor_name')}"
    msg["From"] = smtp_email
    msg["To"] = admin_email

    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
      <h2 style="color: #0284c7; margin-top: 0;">🏥 MedCare Hospital — New Appointment Confirmation</h2>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 15px 0;">
      <table style="width: 100%; border-collapse: collapse;">
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Patient Name:</b></td><td style="padding: 8px 0; font-weight: bold;">{booking_data.get('patient_name')}</td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Patient ID:</b></td><td style="padding: 8px 0;"><code>{booking_data.get('patient_uid')}</code></td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Doctor:</b></td><td style="padding: 8px 0;">{booking_data.get('doctor_name')} ({booking_data.get('department')})</td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Reason / Symptoms:</b></td><td style="padding: 8px 0; color: #0284c7;">{booking_data.get('reason', 'General Consultation')}</td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Date & Time:</b></td><td style="padding: 8px 0; color: #0f766e; font-weight: bold;">{booking_data.get('date')} at {booking_data.get('time_slot')}</td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Consultation Fee:</b></td><td style="padding: 8px 0;">₹{booking_data.get('fee')}</td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Invoice Number:</b></td><td style="padding: 8px 0;"><code>{booking_data.get('invoice_number')}</code></td></tr>
        <tr><td style="padding: 8px 0; color: #64748b;"><b>Hospital Location:</b></td><td style="padding: 8px 0;">{booking_data.get('location')}</td></tr>
      </table>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 15px 0;">
      <p style="font-size: 12px; color: #94a3b8; margin-bottom: 0;">This is an automated notification from the MedCare Healthcare Chatbot.</p>
    </div>
    """
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, admin_email, msg.as_string())
            print(f"[NOTIFIER] Email alert sent to {admin_email}")
    except Exception as e:
        print(f"[NOTIFIER WARNING] Failed to send Email alert: {e}")


def notify_admin_booking_completed(booking_data: dict):
    """Trigger Telegram and Email alerts concurrently in a background thread."""
    def _run_notifications():
        send_telegram_alert(booking_data)
        send_email_alert(booking_data)

    threading.Thread(target=_run_notifications, daemon=True).start()

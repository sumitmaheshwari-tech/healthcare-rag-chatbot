"""Billing query tools — retrieve patient bills and bill details."""

import os
import sys
from typing import Annotated
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_db
from database.models import Billing
from database.audit import log_audit_event


@tool
def get_patient_bills(patient_id: str, state: Annotated[dict, InjectedState]) -> str:
    """Get all billing records for a patient including paid, pending, and overdue bills.

    Args:
        patient_id: The patient's string UID.

    Use this when the user asks about their bills, payments, outstanding
    balances, or billing history.
    """
    # Enforce BOLA authorization check
    auth_uid = state.get("authenticated_patient_uid") if state else None
    if not auth_uid or auth_uid != patient_id:
        log_audit_event(
            request_id="tool-call",
            action="VIEW_BILLS",
            status="FAILED",
            patient_uid=patient_id,
            details=f"BOLA mismatch: authenticated as {auth_uid}"
        )
        return "I'm sorry, but you are not authorized to view bills for this patient."

    db = get_db()
    try:
        bills = (
            db.query(Billing)
            .filter(Billing.patient_id == patient_id)
            .order_by(Billing.created_at.desc())
            .all()
        )

        if not bills:
            log_audit_event(
                request_id="tool-call",
                action="VIEW_BILLS",
                status="SUCCESS",
                patient_uid=patient_id,
                details="No bills found."
            )
            return "No billing records found for this patient."

        total_amount = 0.0
        total_paid = 0.0
        total_pending = 0.0

        lines = []
        for b in bills:
            total_amount += b.amount
            total_paid += b.paid
            total_pending += (b.pending or 0)

            status_emoji = {
                "paid": "✅",
                "pending": "⏳",
                "partial": "🔶",
                "overdue": "🔴",
            }.get(b.status.value, "❓")

            line = (
                f"• **{b.bill_number}** {status_emoji} {b.status.value.capitalize()}\n"
                f"  Description: {b.description or 'N/A'}\n"
                f"  Amount: ₹{b.amount:,.0f} | Paid: ₹{b.paid:,.0f} | "
                f"Pending: ₹{(b.pending or 0):,.0f}\n"
                f"  Insurance Covered: ₹{b.insurance_covered:,.0f} | "
                f"Discount: ₹{b.discount:,.0f}\n"
                f"  Date: {b.created_at.strftime('%B %d, %Y') if b.created_at else 'N/A'}"
            )
            lines.append(line)

        summary = (
            f"**💰 Billing Summary:**\n"
            f"Total Billed: ₹{total_amount:,.0f} | "
            f"Total Paid: ₹{total_paid:,.0f} | "
            f"Total Pending: ₹{total_pending:,.0f}\n\n"
            f"**Bill Details:**\n"
        )

        log_audit_event(
            request_id="tool-call",
            action="VIEW_BILLS",
            status="SUCCESS",
            patient_uid=patient_id,
            resource=f"billings/patient/{patient_id}"
        )

        return summary + "\n\n".join(lines)
    except Exception as e:
        return f"Error retrieving bills: {e}"
    finally:
        db.close()


@tool
def get_bill_details(bill_number: str, state: Annotated[dict, InjectedState]) -> str:
    """Get detailed information about a specific bill by its bill number.

    Args:
        bill_number: The bill number (e.g. 'BILL-2026-001').

    Use this when the user asks about a specific bill or wants a breakdown
    of charges.
    """
    db = get_db()
    try:
        bill = db.query(Billing).filter(Billing.bill_number == bill_number).first()
        if not bill:
            return f"Bill '{bill_number}' not found. Please check the bill number."

        # Enforce BOLA authorization check
        auth_uid = state.get("authenticated_patient_uid") if state else None
        if not auth_uid or auth_uid != bill.patient_id:
            log_audit_event(
                request_id="tool-call",
                action="VIEW_BILL_DETAILS",
                status="FAILED",
                patient_uid=bill.patient_id,
                details=f"Unauthorized attempt to view details of bill {bill_number} by user {auth_uid}"
            )
            return "I'm sorry, but you are not authorized to view this bill's details."

        status_emoji = {
            "paid": "✅",
            "pending": "⏳",
            "partial": "🔶",
            "overdue": "🔴",
        }.get(bill.status.value, "❓")

        log_audit_event(
            request_id="tool-call",
            action="VIEW_BILL_DETAILS",
            status="SUCCESS",
            patient_uid=bill.patient_id,
            resource=f"billings/{bill_number}"
        )

        return (
            f"**Bill Details — {bill.bill_number}** {status_emoji}\n\n"
            f"**Description:** {bill.description or 'N/A'}\n"
            f"**Total Amount:** ₹{bill.amount:,.0f}\n"
            f"**Amount Paid:** ₹{bill.paid:,.0f}\n"
            f"**Pending Amount:** ₹{(bill.pending or 0):,.0f}\n"
            f"**Insurance Covered:** ₹{bill.insurance_covered:,.0f}\n"
            f"**Discount:** ₹{bill.discount:,.0f}\n"
            f"**Payment Method:** {bill.payment_method or 'Not yet paid'}\n"
            f"**Status:** {bill.status.value.capitalize()}\n"
            f"**Date:** {bill.created_at.strftime('%B %d, %Y') if bill.created_at else 'N/A'}\n\n"
            f"For billing queries, visit the Billing Desk at Room 105 or call Extension 1234."
        )
    except Exception as e:
        return f"Error retrieving bill details: {e}"
    finally:
        db.close()

"""Booking system job queue (async tasks for bookings, reminders, digests)."""

import asyncio
from datetime import datetime, timedelta
from typing import Optional
import logging

from . import bookings as _bookings
from . import email_stub
from . import twilio_stub

log = logging.getLogger(__name__)


async def send_booking_summary_email(db, booking_id: str) -> bool:
    """Send post-booking summary email to customer + CC owner.

    Returns True if sent successfully, False otherwise.
    """
    try:
        booking = await _bookings.get_booking(db, booking_id)
        if not booking:
            log.warning(f"Booking {booking_id} not found")
            return False

        agent = await db.get_agent(booking["agent_id"])
        if not agent:
            log.warning(f"Agent {booking['agent_id']} not found")
            return False

        # Get booking type config for labels
        booking_config = agent.get("variables", {}).get("booking_config", {})
        config = booking_config.get(booking["booking_type"], {})

        # Enrich booking with display labels
        booking["entity_name"] = config.get("entity_name", "Booking")
        booking["quantity_label"] = config.get("quantity_label", "Quantity")
        booking["icon"] = config.get("icon", "📋")

        # Get CC email
        cc_email = agent.get("post_call_email_to") or agent.get("owner_email")

        # Render template
        template_name = f"{booking['booking_type']}_reservation.html"
        html = await _render_template(template_name, {
            "booking": booking,
            "agent_name": agent.get("name", ""),
            "payment_link": f"https://pay.spiderx.ai/{booking['payment_link_id']}",
        })

        # Send email
        subject = f"Your {booking['entity_name']} — Booking Confirmation"
        recipients = [booking["customer_email"]] if booking.get("customer_email") else []
        cc = [cc_email] if cc_email else []

        if not recipients:
            log.warning(f"No customer email for booking {booking_id}")
            return False

        success = await email_stub._send(
            to=recipients,
            cc=cc,
            subject=subject,
            html=html,
        )

        if success:
            await db.execute(
                "UPDATE bookings SET summary_email_sent_at = now(), summary_email_to = :to, summary_email_cc = :cc WHERE id = :booking_id",
                {"to": booking["customer_email"], "cc": cc_email or "", "booking_id": booking_id}
            )

        return success
    except Exception as e:
        log.error(f"Error sending booking summary email for {booking_id}: {e}")
        return False


async def send_booking_sms(db, booking_id: str, payment_link: str) -> bool:
    """Send SMS to customer with payment link.

    Returns True if sent successfully, False otherwise.
    """
    try:
        booking = await _bookings.get_booking(db, booking_id)
        if not booking:
            log.warning(f"Booking {booking_id} not found")
            return False

        agent = await db.get_agent(booking["agent_id"])
        if not agent:
            log.warning(f"Agent {booking['agent_id']} not found")
            return False

        # Build SMS message
        agent_name = agent.get("name", "Restaurant")
        message = f"Hi {booking['customer_name']}! Complete your booking at {agent_name}: {payment_link} (expires in 2 hours)"

        # Send SMS
        success = await twilio_stub.send_sms(
            to=booking["customer_phone"],
            body=message,
        )

        if success:
            await db.execute(
                "UPDATE bookings SET sms_sent_at = now(), sms_delivery_status = 'sent', payment_link_expires_at = now() + interval '2 hours' WHERE id = :booking_id",
                {"booking_id": booking_id}
            )

        return success
    except Exception as e:
        log.error(f"Error sending SMS for booking {booking_id}: {e}")
        return False


async def send_payment_reminder(db, booking_id: str) -> bool:
    """Send payment reminder email if payment still pending."""
    try:
        booking = await _bookings.get_booking(db, booking_id)
        if not booking:
            log.warning(f"Booking {booking_id} not found")
            return False

        # Only send if payment still pending
        if booking["payment_status"] != "pending":
            log.info(f"Booking {booking_id} payment already {booking['payment_status']}, skipping reminder")
            return True

        agent = await db.get_agent(booking["agent_id"])
        booking_config = agent.get("variables", {}).get("booking_config", {})
        config = booking_config.get(booking["booking_type"], {})

        booking["entity_name"] = config.get("entity_name", "Booking")
        booking["quantity_label"] = config.get("quantity_label", "Quantity")
        booking["icon"] = config.get("icon", "📋")

        html = await _render_template("payment_reminder.html", {
            "booking": booking,
            "agent_name": agent.get("name", ""),
            "payment_link": f"https://pay.spiderx.ai/{booking['payment_link_id']}",
        })

        success = await email_stub._send(
            to=[booking["customer_email"]],
            subject=f"Payment reminder: Complete your {booking['entity_name']}",
            html=html,
        )

        if success:
            await db.execute(
                "UPDATE booking_reminders SET sent_at = now(), status = 'sent' WHERE booking_id = :booking_id AND reminder_type = 'payment_reminder'",
                {"booking_id": booking_id}
            )

        return success
    except Exception as e:
        log.error(f"Error sending payment reminder for {booking_id}: {e}")
        return False


async def send_booking_day_reminder(db, booking_id: str) -> bool:
    """Send 'your booking is tomorrow' reminder email."""
    try:
        booking = await _bookings.get_booking(db, booking_id)
        if not booking:
            log.warning(f"Booking {booking_id} not found")
            return False

        agent = await db.get_agent(booking["agent_id"])
        booking_config = agent.get("variables", {}).get("booking_config", {})
        config = booking_config.get(booking["booking_type"], {})

        booking["entity_name"] = config.get("entity_name", "Booking")
        booking["quantity_label"] = config.get("quantity_label", "Quantity")
        booking["icon"] = config.get("icon", "📋")

        html = await _render_template("booking_day_reminder.html", {
            "booking": booking,
            "agent_name": agent.get("name", ""),
        })

        success = await email_stub._send(
            to=[booking["customer_email"]],
            subject=f"Your {booking['entity_name']} is tomorrow!",
            html=html,
        )

        if success:
            await db.execute(
                "UPDATE booking_reminders SET sent_at = now(), status = 'sent' WHERE booking_id = :booking_id AND reminder_type = 'booking_day_reminder'",
                {"booking_id": booking_id}
            )

        return success
    except Exception as e:
        log.error(f"Error sending booking day reminder for {booking_id}: {e}")
        return False


async def send_review_request(db, booking_id: str) -> bool:
    """Send post-booking review request email."""
    try:
        booking = await _bookings.get_booking(db, booking_id)
        if not booking:
            log.warning(f"Booking {booking_id} not found")
            return False

        agent = await db.get_agent(booking["agent_id"])
        booking_config = agent.get("variables", {}).get("booking_config", {})
        config = booking_config.get(booking["booking_type"], {})

        booking["entity_name"] = config.get("entity_name", "Booking")
        booking["quantity_label"] = config.get("quantity_label", "Quantity")
        booking["icon"] = config.get("icon", "📋")

        # Customize review prompt per sector
        review_prompts = {
            "restaurant_table": "How was your meal?",
            "salon_appointment": "How was your appointment?",
            "dental_appointment": "How was your procedure?",
            "auto_service": "How was your service?",
            "coaching_session": "How was your session?",
        }
        booking["review_prompt"] = review_prompts.get(booking["booking_type"], "How was your booking?")

        html = await _render_template("review_request.html", {
            "booking": booking,
            "agent_name": agent.get("name", ""),
        })

        success = await email_stub._send(
            to=[booking["customer_email"]],
            subject=f"We'd love your feedback!",
            html=html,
        )

        if success:
            await db.execute(
                "UPDATE booking_reminders SET sent_at = now(), status = 'sent' WHERE booking_id = :booking_id AND reminder_type = 'review_request'",
                {"booking_id": booking_id}
            )

        return success
    except Exception as e:
        log.error(f"Error sending review request for {booking_id}: {e}")
        return False


async def send_daily_digest(db, agent_id: int) -> bool:
    """Send daily booking digest to agent owner (6 PM IST).

    Aggregates all bookings created today by type, status, payment status.
    Lists upcoming bookings for next 7 days.
    """
    try:
        agent = await db.get_agent(agent_id)
        if not agent:
            log.warning(f"Agent {agent_id} not found")
            return False

        recipient = agent.get("post_call_email_to") or agent.get("owner_email")
        if not recipient:
            log.warning(f"No email configured for agent {agent_id}")
            return False

        # Get bookings created today
        today = datetime.now().date()
        bookings_today = await db.fetch(
            """SELECT * FROM bookings
               WHERE agent_id = :agent_id
               AND DATE(created_at) = :date
               ORDER BY booking_date, booking_time""",
            {"agent_id": agent_id, "date": today}
        )

        # Get upcoming bookings (next 7 days)
        future_cutoff = today + timedelta(days=7)
        upcoming = await db.fetch(
            """SELECT * FROM bookings
               WHERE agent_id = :agent_id
               AND DATE(booking_date) BETWEEN :today AND :future
               AND DATE(booking_date) > :today
               AND status != 'cancelled'
               ORDER BY booking_date, booking_time""",
            {"agent_id": agent_id, "today": today, "future": future_cutoff}
        )

        # Aggregate by booking type
        booking_config = agent.get("variables", {}).get("booking_config", {})

        summary_by_type = {}
        for booking in bookings_today:
            btype = booking["booking_type"]
            if btype not in summary_by_type:
                config = booking_config.get(btype, {})
                summary_by_type[btype] = {
                    "entity_name": config.get("entity_name", "Booking"),
                    "icon": config.get("icon", "📋"),
                    "total": 0,
                    "confirmed": 0,
                    "pending_payment": 0,
                }

            summary_by_type[btype]["total"] += 1
            if booking["status"] == "confirmed":
                summary_by_type[btype]["confirmed"] += 1
            if booking["payment_status"] == "pending":
                summary_by_type[btype]["pending_payment"] += 1

        # Render template
        html = await _render_template("daily_digest.html", {
            "agent_name": agent.get("name", ""),
            "today": today,
            "summary_by_type": summary_by_type,
            "upcoming_bookings": upcoming,
            "booking_config": booking_config,
        })

        success = await email_stub._send(
            to=[recipient],
            subject=f"Daily Booking Summary — {agent.get('name', 'Agent')} — {today}",
            html=html,
        )

        if success:
            log.info(f"Daily digest sent for agent {agent_id}")

        return success
    except Exception as e:
        log.error(f"Error sending daily digest for agent {agent_id}: {e}")
        return False


async def send_pending_reminders(db) -> None:
    """Process all pending booking reminders (scheduled every minute by scheduler).

    Checks for reminders where scheduled_for <= now and status = 'pending',
    fires the appropriate job, marks as sent.
    """
    try:
        now = datetime.utcnow()
        pending = await db.fetch(
            """SELECT * FROM booking_reminders
               WHERE scheduled_for <= :now AND status = 'pending'
               ORDER BY scheduled_for ASC""",
            {"now": now}
        )

        for reminder in pending:
            try:
                booking = await _bookings.get_booking(db, reminder["booking_id"])
                if not booking:
                    await db.execute(
                        "UPDATE booking_reminders SET status = 'skipped' WHERE id = :id",
                        {"id": reminder["id"]}
                    )
                    continue

                success = False
                if reminder["reminder_type"] == "payment_reminder":
                    success = await send_payment_reminder(db, reminder["booking_id"])
                elif reminder["reminder_type"] == "booking_day_reminder":
                    success = await send_booking_day_reminder(db, reminder["booking_id"])
                elif reminder["reminder_type"] == "review_request":
                    success = await send_review_request(db, reminder["booking_id"])

                if not success:
                    await db.execute(
                        "UPDATE booking_reminders SET status = 'failed' WHERE id = :id",
                        {"id": reminder["id"]}
                    )
            except Exception as e:
                log.error(f"Error processing reminder {reminder['id']}: {e}")
                await db.execute(
                    "UPDATE booking_reminders SET status = 'failed', failure_reason = :reason WHERE id = :id",
                    {"reason": str(e)[:500], "id": reminder["id"]}
                )

    except Exception as e:
        log.error(f"Error in send_pending_reminders: {e}")


async def send_daily_digests(db) -> None:
    """Send daily digest to all agents with bookings created today.

    Scheduled to run at 6 PM IST daily.
    """
    try:
        # Get all agents that have booking config enabled
        agents = await db.fetch(
            """SELECT id FROM agents
               WHERE variables->>'booking_config' IS NOT NULL
               AND variables->'booking_config'->>'enabled' = 'true'"""
        )

        for agent in agents:
            try:
                await send_daily_digest(db, agent["id"])
            except Exception as e:
                log.error(f"Error sending daily digest for agent {agent['id']}: {e}")
    except Exception as e:
        log.error(f"Error in send_daily_digests: {e}")


async def _render_template(template_name: str, context: dict) -> str:
    """Render a Jinja2 template with context.

    Loads from backend/templates/emails/{template_name}
    """
    try:
        from jinja2 import Environment, FileSystemLoader
        import os

        template_dir = os.path.join(os.path.dirname(__file__), "templates", "emails")
        env = Environment(loader=FileSystemLoader(template_dir))
        template = env.get_template(template_name)
        return template.render(**context)
    except Exception as e:
        log.error(f"Error rendering template {template_name}: {e}")
        return f"<p>Booking confirmation for {context.get('booking', {}).get('customer_name', 'Customer')}</p>"

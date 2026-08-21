"""Multi-sector booking system (restaurant, salon, dental, auto, coaching)."""

import json
from datetime import datetime, timedelta, date, time
from typing import Optional, Dict, Any
from uuid import uuid4
import asyncio

from sqlalchemy import text
import logging

log = logging.getLogger(__name__)

# Booking type configuration schema
BOOKING_TYPE_SCHEMA = {
    "restaurant_table": {
        "enabled": True,
        "entity_name": "Table Reservation",
        "quantity_label": "Party Size",
        "quantity_min": 1,
        "quantity_max": 20,
        "duration_minutes": 120,
        "hold_duration_minutes": 120,
        "notes_label": "Special Requests",
        "notes_placeholder": "Dietary restrictions, seating preference, special occasion",
        "icon": "🪑",
        "metadata_required_fields": [],
        "email_template": "restaurant_reservation",
        "reminder_timings": {
            "payment_reminder_hours": 1,
            "booking_day_reminder_hours": 24,
            "review_request_hours": 24,
        }
    },
    "salon_appointment": {
        "enabled": True,
        "entity_name": "Appointment",
        "quantity_label": "Number of People",
        "quantity_min": 1,
        "quantity_max": 5,
        "duration_minutes": 60,
        "hold_duration_minutes": 60,
        "notes_label": "Preferred Services",
        "notes_placeholder": "Haircut, color, styling, treatments",
        "icon": "💇",
        "metadata_required_fields": [],
        "email_template": "salon_appointment",
        "reminder_timings": {
            "payment_reminder_hours": 1,
            "booking_day_reminder_hours": 24,
            "review_request_hours": 24,
        }
    },
    "dental_appointment": {
        "enabled": True,
        "entity_name": "Procedure",
        "quantity_label": "Patients",
        "quantity_min": 1,
        "quantity_max": 2,
        "duration_minutes": 45,
        "hold_duration_minutes": 30,
        "notes_label": "Procedure Notes",
        "notes_placeholder": "Checkup, cleaning, extraction, root canal",
        "icon": "🦷",
        "metadata_required_fields": [],
        "email_template": "dental_appointment",
        "reminder_timings": {
            "payment_reminder_hours": 1,
            "booking_day_reminder_hours": 24,
            "review_request_hours": 48,
        }
    },
    "auto_service": {
        "enabled": True,
        "entity_name": "Service Appointment",
        "quantity_label": "Vehicles",
        "quantity_min": 1,
        "quantity_max": 3,
        "duration_minutes": 120,
        "hold_duration_minutes": 180,
        "notes_label": "Service Requirements",
        "notes_placeholder": "Oil change, tire rotation, AC service",
        "icon": "🚗",
        "metadata_required_fields": [],
        "email_template": "auto_service",
        "reminder_timings": {
            "payment_reminder_hours": 2,
            "booking_day_reminder_hours": 24,
            "review_request_hours": 48,
        }
    },
    "coaching_session": {
        "enabled": True,
        "entity_name": "Session",
        "quantity_label": "Sessions",
        "quantity_min": 1,
        "quantity_max": 10,
        "duration_minutes": 60,
        "hold_duration_minutes": 60,
        "notes_label": "Coaching Goals",
        "notes_placeholder": "Career guidance, skill development, mentoring",
        "icon": "🎯",
        "metadata_required_fields": [],
        "email_template": "coaching_session",
        "reminder_timings": {
            "payment_reminder_hours": 1,
            "booking_day_reminder_hours": 24,
            "review_request_hours": 24,
        }
    },
}


async def validate_metadata(booking_type: str, metadata: Dict[str, Any]) -> bool:
    """Validate metadata against schema for booking_type."""
    schema = BOOKING_TYPE_SCHEMA.get(booking_type)
    if not schema:
        raise ValueError(f"Unknown booking_type: {booking_type}")

    required_fields = schema.get("metadata_required_fields", [])
    for field in required_fields:
        if field not in metadata:
            raise ValueError(f"Missing required metadata field: {field}")

    return True


async def create_booking(
    db,
    call_id: str,
    agent_id: int,
    org_id: int,
    booking_type: str,
    customer_name: str,
    customer_phone: str,
    customer_email: Optional[str],
    quantity: int,
    booking_date: date,
    booking_time: time,
    special_notes: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a new booking (generic, multi-sector).

    Returns:
        Booking dict with id, status, payment_link, held_until
    """
    booking_id = str(uuid4())
    metadata = metadata or {}

    # Validate metadata against schema
    await validate_metadata(booking_type, metadata)

    # Get booking config for this type
    schema = BOOKING_TYPE_SCHEMA[booking_type]
    duration_minutes = schema.get("duration_minutes", 60)
    hold_duration_minutes = schema.get("hold_duration_minutes", 120)

    # Calculate held_until
    held_until = datetime.now().__add__(timedelta(minutes=hold_duration_minutes))

    # Insert booking
    booking = {
        "id": booking_id,
        "call_id": call_id,
        "agent_id": agent_id,
        "org_id": org_id,
        "booking_type": booking_type,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "customer_email": customer_email,
        "quantity": quantity,
        "booking_date": booking_date,
        "booking_time": booking_time,
        "duration_minutes": duration_minutes,
        "special_notes": special_notes,
        "metadata": json.dumps(metadata) if metadata else "{}",
        "status": "hold",
        "held_at": datetime.now(),
        "held_until": held_until,
        "payment_link_id": booking_id,
        "payment_link_expires_at": datetime.now() + timedelta(hours=2),
        "payment_status": "pending",
    }

    # Insert into DB
    await db.execute(
        """
        INSERT INTO bookings
        (id, call_id, agent_id, org_id, booking_type, customer_name, customer_phone,
         customer_email, quantity, booking_date, booking_time, duration_minutes,
         special_notes, metadata, status, held_at, held_until, payment_link_id,
         payment_link_expires_at, payment_status)
        VALUES (:id, :call_id, :agent_id, :org_id, :booking_type, :customer_name,
                :customer_phone, :customer_email, :quantity, :booking_date, :booking_time,
                :duration_minutes, :special_notes, :metadata, :status, :held_at, :held_until,
                :payment_link_id, :payment_link_expires_at, :payment_status)
        """,
        booking
    )

    log.info(f"Booking created: {booking_id} ({booking_type})")

    return {
        "booking_id": booking_id,
        "status": "hold",
        "payment_link": f"https://pay.spiderx.ai/{booking_id}",
        "held_until": held_until.isoformat(),
    }


async def schedule_reminders(
    db,
    booking_id: str,
    booking_type: str,
    booking_date: date,
    booking_time: time,
    reminder_config: Dict[str, int],
) -> int:
    """
    Schedule reminders for a booking based on config.

    reminder_config = {
        "payment_reminder_hours": 1,
        "booking_day_reminder_hours": 24,
        "review_request_hours": 24
    }

    Returns:
        Number of reminders scheduled
    """
    reminders = []
    now = datetime.now()

    # Payment reminder (1h after creation)
    if reminder_config.get("payment_reminder_hours"):
        reminders.append({
            "booking_id": booking_id,
            "reminder_type": "payment_reminder",
            "scheduled_for": now + timedelta(
                hours=reminder_config["payment_reminder_hours"]
            ),
            "status": "pending",
        })

    # Booking day reminder (N hours before booking time)
    if reminder_config.get("booking_day_reminder_hours"):
        booking_dt = datetime.combine(booking_date, booking_time)
        reminders.append({
            "booking_id": booking_id,
            "reminder_type": "booking_day_reminder",
            "scheduled_for": booking_dt - timedelta(
                hours=reminder_config["booking_day_reminder_hours"]
            ),
            "status": "pending",
        })

    # Review request (N hours after booking time)
    if reminder_config.get("review_request_hours"):
        booking_dt = datetime.combine(booking_date, booking_time)
        reminders.append({
            "booking_id": booking_id,
            "reminder_type": "review_request",
            "scheduled_for": booking_dt + timedelta(
                hours=reminder_config["review_request_hours"]
            ),
            "status": "pending",
        })

    # Insert all reminders
    for r in reminders:
        await db.execute(
            """
            INSERT INTO booking_reminders
            (booking_id, reminder_type, scheduled_for, status)
            VALUES (:booking_id, :reminder_type, :scheduled_for, :status)
            """,
            r
        )

    log.info(f"Scheduled {len(reminders)} reminders for booking {booking_id}")
    return len(reminders)


async def get_booking(db, booking_id: str) -> Dict[str, Any]:
    """Get a booking by ID."""
    result = await db.fetch_one(
        "SELECT * FROM bookings WHERE id = :id",
        {"id": booking_id}
    )
    if result:
        result["metadata"] = json.loads(result["metadata"]) if result["metadata"] else {}
    return result


async def list_bookings(
    db,
    agent_id: int,
    booking_type: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple:
    """
    List bookings with optional filters.

    Returns:
        (bookings list, total count)
    """
    filters = ["agent_id = :agent_id"]
    params = {"agent_id": agent_id}

    if booking_type:
        filters.append("booking_type = :booking_type")
        params["booking_type"] = booking_type

    if status:
        filters.append("status = :status")
        params["status"] = status

    if date_from:
        filters.append("booking_date >= :date_from::date")
        params["date_from"] = date_from

    where_clause = " AND ".join(filters) if filters else "1=1"

    # Get total count
    count_result = await db.fetch_one(
        f"SELECT COUNT(*) as cnt FROM bookings WHERE {where_clause}",
        params
    )
    total = count_result["cnt"] if count_result else 0

    # Get bookings
    results = await db.fetch(
        f"""
        SELECT * FROM bookings
        WHERE {where_clause}
        ORDER BY booking_date DESC, booking_time DESC
        LIMIT :limit OFFSET :offset
        """,
        {**params, "limit": limit, "offset": offset}
    )

    # Parse metadata
    for booking in results:
        booking["metadata"] = json.loads(booking["metadata"]) if booking["metadata"] else {}

    return results, total


async def update_booking_status(
    db,
    booking_id: str,
    status: str,
    **kwargs
) -> bool:
    """Update booking status and optional fields."""
    updates = {"status": status, **kwargs}
    update_fields = ", ".join([f"{k} = :{k}" for k in updates.keys()])

    await db.execute(
        f"""
        UPDATE bookings
        SET {update_fields}, updated_at = now()
        WHERE id = :booking_id
        """,
        {**updates, "booking_id": booking_id}
    )

    log.info(f"Updated booking {booking_id} status to {status}")
    return True


async def update_booking_payment_status(
    db,
    booking_id: str,
    payment_status: str,
    **kwargs
) -> bool:
    """Update booking payment status."""
    updates = {"payment_status": payment_status, **kwargs}
    update_fields = ", ".join([f"{k} = :{k}" for k in updates.keys()])

    await db.execute(
        f"""
        UPDATE bookings
        SET {update_fields}, updated_at = now()
        WHERE id = :booking_id
        """,
        {**updates, "booking_id": booking_id}
    )

    log.info(f"Updated booking {booking_id} payment status to {payment_status}")
    return True


def get_booking_labels(booking_type: str) -> Dict[str, Any]:
    """Get display labels for a booking type."""
    return BOOKING_TYPE_SCHEMA.get(booking_type, {})

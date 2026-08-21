# Restaurant Booking System Configuration

## Overview

Restaurant agents can enable booking functionality by setting `booking_config` in their agent variables.

## Enabling Bookings for Restaurant Agents

### Step 1: Update Agent Variables

Add this to the agent's `variables` JSON field:

```json
{
  "booking_config": {
    "restaurant_table": {
      "enabled": true,
      "entity_name": "Table Reservation",
      "quantity_label": "Party Size",
      "quantity_min": 1,
      "quantity_max": 20,
      "duration_minutes": 120,
      "hold_duration_minutes": 120,
      "notes_label": "Special Requests",
      "notes_placeholder": "Dietary restrictions, seating preference, special occasion",
      "icon": "🪑",
      "email_template": "restaurant_reservation",
      "reminder_timings": {
        "payment_reminder_hours": 1,
        "booking_day_reminder_hours": 24,
        "review_request_hours": 24
      }
    }
  }
}
```

### Step 2: Configure Owner Email

Set the `post_call_email_to` field on the agent:

```json
{
  "post_call_email_to": "owner@restaurant.com"
}
```

This email will receive:
- **Per-booking summary email** (CC'd when customer gets their booking confirmation)
- **Daily digest email** (sent at 6 PM IST with all bookings created that day)

## How It Works

### 1. Booking Creation

When a customer books via voice call:
- Agent captures: name, phone, email, party size, date, time, special requests
- Booking is created with status `hold` (temporarily reserved)
- SMS sent to customer with payment link
- Email sent to customer (CC'd to owner) with booking details

### 2. Booking Lifecycle

```
hold (just created)
  ↓
confirmed (customer enters payment details or owner approves)
  ↓
completed (booking time has passed) OR no_show OR cancelled
```

### 3. Payment Status Tracking

```
pending (no action yet)
  ↓
link_sent (SMS with payment link sent)
  ↓
link_visited (customer clicked the link)
  ↓
card_entered (customer entered card details)
  ↓
confirmed (payment received or owner approved)
```

## Example Booking Flow

**1. Call ends with booking**
```
Agent: "I've booked a table for 4 on August 25 at 7:30 PM under Priya Sharma. You'll receive an SMS with a payment link shortly."
↓
POST /api/bookings {
  "call_id": "uuid",
  "agent_id": 47,
  "booking_type": "restaurant_table",
  "customer_name": "Priya Sharma",
  "customer_phone": "+919876543210",
  "customer_email": "priya@example.com",
  "quantity": 4,
  "booking_date": "2026-08-25",
  "booking_time": "19:30",
  "special_notes": "Window seat, no nuts"
}
```

**2. Customer receives SMS + Email**
```
SMS: "Hi Priya! Complete your booking at [Restaurant]: https://pay.spiderx.ai/{booking_id} (expires in 2 hours)"

Email (to: priya@example.com, cc: owner@restaurant.com):
  Subject: Your reservation at [Restaurant] — Booking Confirmation
  Body: Table for 4 on Aug 25 at 7:30 PM | Status: On Hold
         Click to confirm: [Payment Link]
```

**3. Owner receives daily digest**
```
Email (to: owner@restaurant.com, 6 PM IST):
  Subject: Daily Booking Summary — [Restaurant] — 2026-08-21
  
  📊 Bookings Today: 8
  💳 Payment Pending: 2
  ✅ Payment Confirmed: 5
  ❌ Cancelled: 1
  
  Upcoming (Next 7 days):
  - Aug 22, 7:30 PM: Sharma (4) — Confirmed
  - Aug 23, 8:00 PM: Patel (6) — Pending Payment
```

## Restaurant Dashboard

Access bookings at: `/agent/{slug}/bookings`

**Features:**
- View all bookings with status (hold, confirmed, completed, cancelled, no-show)
- Filter by date, booking type, payment status
- See customer details and notes
- Track payment confirmation
- Quick actions: confirm, cancel, mark as no-show

## Customization

### Change Hold Duration

Adjust `hold_duration_minutes` (default: 120 for restaurants):

```json
{
  "hold_duration_minutes": 180  // 3 hours instead of 2
}
```

### Change Reminder Timings

Adjust when reminders are sent:

```json
{
  "reminder_timings": {
    "payment_reminder_hours": 2,     // Send payment reminder 2h after booking
    "booking_day_reminder_hours": 24, // Send "Your booking is tomorrow" 24h before
    "review_request_hours": 24       // Send "How was your meal?" 24h after booking time
  }
}
```

### Change Email Labels

Customize the display text:

```json
{
  "entity_name": "Restaurant Reservation",
  "quantity_label": "Number of Guests",
  "notes_label": "Special Requests & Dietary Info"
}
```

## Database Schema

### bookings table

```sql
id UUID                    -- Unique booking ID
call_id UUID              -- Link to voice call
agent_id INT              -- Which agent booked this
org_id INT                -- Which restaurant org
booking_type VARCHAR(50)  -- Always "restaurant_table" for restaurants
customer_name VARCHAR     -- Guest name
customer_phone VARCHAR    -- Phone number
customer_email VARCHAR    -- Email for confirmations
quantity INT              -- Party size
booking_date DATE         -- Date of reservation
booking_time TIME         -- Time of reservation
duration_minutes INT      -- How long the table is held
special_notes TEXT        -- Special requests (no nuts, window seat, etc.)
status VARCHAR(50)        -- hold | confirmed | completed | cancelled | no_show
held_until TIMESTAMPTZ    -- When the hold expires
payment_link_id VARCHAR   -- Unique link for payment
payment_status VARCHAR    -- pending | link_sent | link_visited | card_entered | confirmed
sms_sent_at TIMESTAMPTZ   -- When payment SMS was sent
summary_email_sent_at     -- When booking confirmation email was sent
metadata JSONB            -- Extra data (table_preference, covers, etc.)
created_at TIMESTAMPTZ    -- When booking was created
updated_at TIMESTAMPTZ    -- Last update time
```

### booking_reminders table

```sql
id BIGINT                   -- Reminder ID
booking_id UUID             -- Which booking
reminder_type VARCHAR(50)   -- payment_reminder | booking_day_reminder | review_request
scheduled_for TIMESTAMPTZ  -- When to send
sent_at TIMESTAMPTZ        -- When it was actually sent
status VARCHAR(50)         -- pending | sent | failed | skipped
failure_reason TEXT        -- If failed, why
created_at TIMESTAMPTZ     -- When reminder was scheduled
```

## API Reference

### Create Booking

```
POST /api/bookings

{
  "call_id": "uuid",
  "agent_id": 47,
  "booking_type": "restaurant_table",
  "customer_name": "Priya Sharma",
  "customer_phone": "+919876543210",
  "customer_email": "priya@example.com",
  "quantity": 4,
  "booking_date": "2026-08-25",
  "booking_time": "19:30",
  "special_notes": "Window seat preferred",
  "metadata": {
    "table_preference": "outdoor"
  }
}

Response 201:
{
  "booking_id": "uuid",
  "status": "hold",
  "payment_link": "https://pay.spiderx.ai/uuid",
  "held_until": "2026-08-21T17:00:00Z"
}
```

### List Bookings

```
GET /api/agent/{agent_id}/bookings?booking_type=restaurant_table&status=hold&date_from=2026-08-21

Response 200:
{
  "bookings": [
    {
      "id": "uuid",
      "customer_name": "Priya Sharma",
      "quantity": 4,
      "booking_date": "2026-08-25",
      "booking_time": "19:30",
      "status": "confirmed",
      "payment_status": "card_entered",
      "entity_name": "Table Reservation",
      "quantity_label": "Party Size",
      "icon": "🪑"
    }
  ],
  "total": 45,
  "summary": {
    "total_hold": 2,
    "total_confirmed": 5,
    "payment_pending": 1,
    "payment_completed": 4
  }
}
```

## Migration Path

For existing restaurants:

1. **Review current workflow** — Do restaurants currently use Seven Rooms or another system?
2. **Opt-in** — Only enable bookings for restaurants that want the feature
3. **Configure** — Set `booking_config` and `post_call_email_to`
4. **Test** — Make a test booking to verify SMS, email, and dashboard work
5. **Go live** — Customers can now book via voice

## Future Enhancements

- **Real payment processing** (Razorpay/Stripe integration)
- **Seven Rooms sync** (push confirmed bookings to their system)
- **WhatsApp reminders** (instead of/in addition to SMS)
- **Review collection** (ask customers to review post-dining)
- **Cancellation handling** (auto-refund if implemented)
- **Dynamic pricing** (adjust booking fee based on demand)

---

## Questions?

For issues or questions about restaurant bookings, reach out to the ops team or create a GitHub issue.

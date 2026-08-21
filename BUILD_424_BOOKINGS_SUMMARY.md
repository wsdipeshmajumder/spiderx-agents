# Build 424: Multi-Sector Booking System (Implementation Plan)

## ✅ Completed

### 1. Database Schema (Migration 0037)
- ✅ `bookings` table (36 columns, generic multi-sector design)
- ✅ `booking_reminders` table (8 columns, auto-scheduled reminders)
- ✅ Indexes on agent_id, booking_type, status, payment_status, held_until
- ✅ Foreign key constraints + cascade deletes
- ✅ JSONB metadata field for sector-specific data

### 2. Backend Business Logic (`backend/bookings.py`)
- ✅ `create_booking()` — Generic creation with metadata validation
- ✅ `validate_metadata()` — Schema validation per booking_type
- ✅ `schedule_reminders()` — Auto-schedule 3 reminder types
- ✅ `get_booking()`, `list_bookings()` — Query operations
- ✅ `update_booking_status()`, `update_booking_payment_status()` — Status updates
- ✅ `BOOKING_TYPE_SCHEMA` — Config for restaurant, salon, dental, auto, coaching
- ✅ Built-in label mapping for multi-sector display names

### 3. API Endpoints (`backend/app.py`)
- ✅ `POST /api/bookings` — Create booking (generic, multi-sector)
  - Validates booking_type enabled for agent
  - Auto-schedules reminders based on sector config
  - Returns payment_link + booking_id
- ✅ `GET /api/agent/{agent_id}/bookings` — List bookings
  - Filters: booking_type, status, date_from
  - Enriches with display labels
  - Returns summary metrics (hold, confirmed, payment breakdown)

### 4. Configuration & Documentation
- ✅ `BOOKING_CONFIG.md` — Complete setup guide
  - Agent variable schema
  - Configuration examples
  - Flow diagrams
  - Dashboard access
  - Customization options
  - API reference

### 5. Booking Type Schemas

Each sector has a pre-configured schema with customizable properties:

| Sector | Entity | Hold Duration | Reminder Timings |
|---|---|---|---|
| **restaurant_table** | Table Reservation | 120 min | 1h, 24h, 24h+ |
| **salon_appointment** | Appointment | 60 min | 1h, 24h, 24h+ |
| **dental_appointment** | Procedure | 30 min | 1h, 24h, 48h+ |
| **auto_service** | Service Appointment | 180 min | 2h, 24h, 48h+ |
| **coaching_session** | Session | 60 min | 1h, 24h, 48h+ |

---

## 🚧 In Progress / To-Do

### Sprint 1: Email & SMS Job Queues

**Status: Not yet implemented**

```python
# Jobs to implement in backend/jobs.py (or Celery task file)

1. send_booking_summary_email(booking_id)
   - Get booking + agent
   - Get sector-specific email template
   - Render with booking labels (e.g., "Party Size" for restaurant)
   - Send to customer_email + CC to post_call_email_to
   - Mark summary_email_sent_at

2. send_booking_sms(booking_id, payment_link)
   - Get booking
   - Send SMS with payment link via Twilio
   - Mark sms_sent_at + sms_delivery_status
   - Update payment_link_expires_at

3. send_payment_reminder(booking_id)
   - Check if payment_status still pending
   - Send reminder email with payment link
   - Mark reminder as sent

4. send_booking_day_reminder(booking_id)
   - Send "Your booking is tomorrow" email
   - Include booking time, location, details

5. send_review_request(booking_id)
   - Send post-booking review request email
   - Customize per sector (restaurant: "How was your meal?", salon: "How was your appointment?")
```

### Sprint 1: Email Templates

**Location: `backend/templates/emails/`**

Required templates (support sector-specific labels via context):

```
1. restaurant_reservation.html
   - Customized for restaurant
   - Shows "Table for {quantity}", "Party Size", etc.

2. salon_appointment.html
   - Customized for salon
   - Shows "Appointment for {quantity}", "Services", etc.

3. dental_appointment.html
4. auto_service.html
5. coaching_session.html
6. payment_reminder.html (generic, uses labels)
7. booking_day_reminder.html (generic, uses labels)
8. review_request.html (generic, uses labels)
9. daily_digest.html (aggregated bookings by sector)
```

**Template pattern (all use booking.entity_name, booking.icon, etc. from config):**

```html
<h2>{icon} Your {entity_name}</h2>
<p>{quantity_label}: {booking.quantity}</p>
<p>Date: {booking.booking_date}</p>
<p>Time: {booking.booking_time}</p>
<p><a href="{payment_link}">Confirm Your Booking</a></p>
```

### Sprint 2: Daily Digest Job

**Status: Not yet implemented**

```python
# Job: send_daily_digest(agent_id)
# Runs at 6 PM IST each day

- Query all bookings created today
- Group by booking_type
- Aggregate: total, confirmed, payment_pending
- Get upcoming bookings (next 7 days)
- Render digest template
- Send to post_call_email_to (or agent owner_email)
```

### Sprint 2: Restaurant Dashboard Frontend

**Location: `frontend/app.js`**

New page: `/agent/{slug}/bookings`

Features needed:
- Booking list view with dynamic labels (based on booking_type)
- Filters: booking_type, status, payment_status, date range
- Each row shows: customer, party size, date/time, status, payment status
- Summary metrics cards (total, confirmed, pending, completed)
- Quick actions: view details, confirm, cancel, mark no-show
- Export CSV for accounting

---

## 📋 Restaurant Booking Add-On (Feature Flag)

As discussed, bookings should be an **optional add-on feature**, similar to the chat add-on.

### Implementation in agent `variables`:

```json
{
  "booking_config": {
    "enabled": true,  // Feature flag
    "restaurant_table": {
      "enabled": true,
      "entity_name": "Table Reservation",
      // ... rest of config
    }
  },
  "post_call_email_to": "owner@restaurant.com"
}
```

### Frontend Feature Gate:

In UI, only show booking features if:
```javascript
agent.variables?.booking_config?.enabled === true &&
agent.variables?.booking_config?.restaurant_table?.enabled === true
```

---

## 🔄 Current Booking Flow (Skeleton)

```
1. Agent creates booking via /api/bookings endpoint
   ✅ Endpoint ready
   ✅ DB insert ready
   ✅ Reminders scheduled ✅

2. Queue sends SMS with payment link
   🚧 Job not yet queued
   🚧 SMS template not yet created

3. Queue sends email to customer (CC owner)
   🚧 Job not yet queued
   🚧 Email template not yet created

4. Reminder jobs fire at scheduled times
   🚧 Reminder jobs not yet implemented

5. Customer opens payment link
   🚧 Payment page not yet implemented
   🚧 For MVP: placeholder page that marks "card_details_entered_at"

6. Restaurant views dashboard
   🚧 Frontend dashboard not yet built
   🚧 But API is ready

7. Daily digest sent at 6 PM IST
   🚧 Digest job not yet implemented
```

---

## 🎯 Next Steps (Ordered by Priority)

### Immediate (MVP, Sprint 1):

1. **Implement email jobs** (send_booking_summary_email, send_payment_reminder)
   - Create template engine that respects booking_config labels
   - Integrate with existing email_stub.py (_send function)
   - Test with mock booking data

2. **Implement SMS job** (send_booking_sms)
   - Use existing Twilio integration
   - Update payment_link_id and sms_sent_at in DB

3. **Create email templates** (Jinja2)
   - restaurant_reservation.html (use booking.entity_name, booking.icon)
   - payment_reminder.html (generic)
   - booking_day_reminder.html (generic)
   - review_request.html (generic)
   - All should pull display labels from config

4. **Set up job queue integration**
   - When POST /api/bookings succeeds, enqueue email + SMS jobs
   - Currently in app.py commented as "TODO: Enqueue..."

5. **Test E2E**
   - Create booking → verify SMS sent + email sent
   - Check booking appears in DB with correct status
   - Verify email has correct labels ("Party Size" for restaurant, etc.)

### Sprint 2 (Reminders + Digest):

6. **Implement reminder jobs**
   - send_payment_reminder
   - send_booking_day_reminder
   - send_review_request

7. **Implement daily digest job**
   - Runs at 6 PM IST
   - Aggregates all bookings created today
   - Shows breakdown by status, payment status
   - Lists upcoming bookings (next 7 days)

8. **Create daily digest template**
   - Format with sector labels (e.g., "Table Reservations: 5 created, 3 confirmed")

### Sprint 3 (Dashboard):

9. **Build restaurant dashboard frontend**
   - `/agent/{slug}/bookings` page
   - List view with filters
   - Summary metrics
   - Quick actions

---

## 🧪 Testing Checklist

- [ ] Create booking for restaurant via API
- [ ] Verify SMS sent to customer with payment link
- [ ] Verify email sent to customer + CC'd to owner
- [ ] Check reminders scheduled in DB
- [ ] Manually trigger payment_reminder job, verify email sent
- [ ] Manually trigger booking_day_reminder job, verify email sent
- [ ] Manually trigger review_request job, verify email sent
- [ ] Manually trigger daily_digest job, verify email sent to owner
- [ ] Dashboard lists booking correctly
- [ ] Filter by status works
- [ ] Filter by booking_type works (when multi-sector added)

---

## 🔧 Configuration Files That Need Updates

### To enable restaurant bookings:

**SQL Insert:**
```sql
-- Update existing restaurant agents (Maya-2, Maya-10)
UPDATE agents
SET variables = jsonb_set(variables, '{booking_config}', '{"restaurant_table": {"enabled": true, ...}}'::jsonb)
WHERE sector = 'restaurant';

UPDATE agents
SET post_call_email_to = 'owner@restaurant.com'
WHERE id IN (16, 47);  -- Maya-2, Maya-10 IDs
```

---

## 📊 Metrics to Track

Once live:

- `bookings_created_count` — per hour, per agent
- `payment_confirmation_rate` — % of SMS → card entry
- `sms_delivery_rate` — % successfully delivered (vs failed)
- `email_send_latency_p99` — target < 30s
- `reminder_execution_rate` — % of scheduled reminders sent on time
- `no_show_rate` — % of confirmed bookings that didn't show up
- `cancellation_rate` — % cancelled before booking time

---

## 🚀 Future Enhancements (Post-MVP)

1. **Real payment processing** — Razorpay/Stripe integration (auto-confirm on payment)
2. **Seven Rooms sync** — Push confirmed bookings to their system
3. **WhatsApp reminders** — Instead of/in addition to SMS
4. **Phone call reminders** — Outbound call 24h before (using phone-ai voice)
5. **Cancellation refunds** — Auto-refund if integrated with payment processor
6. **Dynamic pricing** — Adjust booking fee based on demand/time
7. **Staff scheduling** — Link bookings to staff availability
8. **Review sentiment** — Analyze post-dine reviews for insights
9. **Multi-lang support** — Templates in multiple languages per agent locale
10. **Waitlist** — When fully booked, allow customers to join waitlist

---

## 📝 Build 424 Checklist

- ✅ Database schema (bookings + reminders tables)
- ✅ Business logic layer (bookings.py)
- ✅ API endpoints (create + list)
- ✅ Configuration schema (BOOKING_TYPE_SCHEMA)
- 🚧 Email job queues (in progress)
- 🚧 SMS job queue (in progress)
- 🚧 Reminder job scheduler (in progress)
- 🚧 Email templates (in progress)
- 🚧 Daily digest job (in progress)
- 🚧 Dashboard frontend (in progress)
- 🚧 Multi-sector support (in progress)
- 🚧 End-to-end testing (in progress)

---

## 💾 Database

All data is in Postgres:
- Bookings: stored forever (or per compliance retention policy)
- Reminders: stored until sent + 30 days (for audit)
- External sync (Seven Rooms): optional fields, can be populated later

---

Done! Ready for Sprint 1 implementation. 🎉

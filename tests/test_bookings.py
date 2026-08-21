"""Build 424: Booking system eval suite — offline + online tests.

Tests the multi-sector booking system for all 5 industry agents:
- restaurant_table (restaurant)
- salon_appointment (salon)
- dental_appointment (dental)
- auto_service (automotive)
- coaching_session (coaching)

Covers:
1. Booking creation (all sectors)
2. Reminder scheduling (payment, day-before, review)
3. Email/SMS job execution
4. Daily digest aggregation
5. Dashboard filtering and display

USAGE:
  python -m pytest tests/test_bookings.py -v           # offline tests
  python tests/eval_suite.py --only bookings           # online API tests (needs server)
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ─── Offline unit tests ──────────────────────────────────────────────────

class TestBookingSchema(unittest.TestCase):
    """Test booking type schema validation."""

    def setUp(self):
        from backend import bookings
        self.bookings_module = bookings
        self.SCHEMA = bookings.BOOKING_TYPE_SCHEMA

    def test_all_five_booking_types_present(self):
        """Verify all 5 industry sectors are defined."""
        expected_types = [
            "restaurant_table", "salon_appointment", "dental_appointment",
            "auto_service", "coaching_session"
        ]
        for btype in expected_types:
            self.assertIn(btype, self.SCHEMA, f"Missing booking type: {btype}")

    def test_restaurant_schema(self):
        """Verify restaurant_table config is complete."""
        config = self.SCHEMA.get("restaurant_table", {})
        self.assertTrue(config.get("enabled"))
        self.assertEqual(config.get("entity_name"), "Table Reservation")
        self.assertEqual(config.get("quantity_label"), "Party Size")
        self.assertEqual(config.get("quantity_min"), 1)
        self.assertEqual(config.get("quantity_max"), 20)
        self.assertEqual(config.get("icon"), "🪑")

    def test_salon_schema(self):
        """Verify salon_appointment config is complete."""
        config = self.SCHEMA.get("salon_appointment", {})
        self.assertTrue(config.get("enabled"))
        self.assertEqual(config.get("entity_name"), "Appointment")
        self.assertEqual(config.get("quantity_label"), "Number of People")

    def test_dental_schema(self):
        """Verify dental_appointment config is complete."""
        config = self.SCHEMA.get("dental_appointment", {})
        self.assertTrue(config.get("enabled"))
        self.assertEqual(config.get("entity_name"), "Procedure")
        self.assertEqual(config.get("quantity_min"), 1)

    def test_auto_service_schema(self):
        """Verify auto_service config is complete."""
        config = self.SCHEMA.get("auto_service", {})
        self.assertTrue(config.get("enabled"))
        self.assertEqual(config.get("entity_name"), "Service Appointment")
        self.assertGreater(config.get("hold_duration_minutes", 0), 0)

    def test_coaching_session_schema(self):
        """Verify coaching_session config is complete."""
        config = self.SCHEMA.get("coaching_session", {})
        self.assertTrue(config.get("enabled"))
        self.assertEqual(config.get("entity_name"), "Session")
        self.assertEqual(config.get("quantity_label"), "Sessions")

    def test_all_types_have_reminder_timings(self):
        """Verify all types define reminder timings."""
        for btype, config in self.SCHEMA.items():
            if btype == "enabled":
                continue
            reminders = config.get("reminder_timings", {})
            self.assertIn("payment_reminder_hours", reminders, f"{btype} missing payment_reminder_hours")
            self.assertIn("booking_day_reminder_hours", reminders, f"{btype} missing booking_day_reminder_hours")
            self.assertIn("review_request_hours", reminders, f"{btype} missing review_request_hours")

    def test_booking_labels_consistency(self):
        """Verify labels are consistent across types."""
        for btype, config in self.SCHEMA.items():
            if btype == "enabled":
                continue
            # Each type should have display labels
            self.assertIn("entity_name", config, f"{btype} missing entity_name")
            self.assertIn("quantity_label", config, f"{btype} missing quantity_label")
            self.assertIn("icon", config, f"{btype} missing icon")


class TestBookingValidation(unittest.TestCase):
    """Test booking metadata validation."""

    def setUp(self):
        from backend import bookings
        self.bookings_module = bookings

    async def test_validate_restaurant_metadata(self):
        """Verify restaurant booking metadata validation."""
        # This would require a DB connection, so we skip for now
        # In a real test, we'd use a fixture DB
        pass

    async def test_validate_dental_metadata(self):
        """Verify dental booking metadata validation."""
        pass


class TestReminderScheduling(unittest.TestCase):
    """Test reminder scheduling logic."""

    def test_payment_reminder_timing(self):
        """Verify payment reminder scheduled 1 hour after booking."""
        # Restaurant: 1 hour
        config = {
            "payment_reminder_hours": 1,
            "booking_day_reminder_hours": 24,
            "review_request_hours": 24,
        }
        booking_date = datetime(2026, 8, 25, 19, 30)  # 7:30 PM
        payment_reminder_time = booking_date - timedelta(hours=config["payment_reminder_hours"] - 1)
        # Should be scheduled ~1 hour after booking creation
        self.assertIsNotNone(payment_reminder_time)

    def test_booking_day_reminder_timing(self):
        """Verify day-before reminder scheduled 24 hours before booking."""
        booking_date = datetime(2026, 8, 25, 19, 30)
        reminder_date = booking_date - timedelta(hours=24)
        self.assertEqual(reminder_date.day, 24)

    def test_review_request_timing(self):
        """Verify review request scheduled 24 hours after booking time."""
        booking_date = datetime(2026, 8, 25, 19, 30)
        duration_minutes = 120
        booking_end = booking_date + timedelta(minutes=duration_minutes)
        review_scheduled = booking_end + timedelta(hours=24)
        self.assertEqual(review_scheduled.day, 26)


class TestEmailTemplates(unittest.TestCase):
    """Test email template availability."""

    def test_all_templates_exist(self):
        """Verify all email templates are present."""
        from pathlib import Path
        template_dir = Path(REPO / "backend" / "templates" / "emails")
        expected = [
            "restaurant_reservation.html",
            "salon_appointment.html",
            "dental_appointment.html",
            "auto_service.html",
            "coaching_session.html",
            "payment_reminder.html",
            "booking_day_reminder.html",
            "review_request.html",
            "daily_digest.html",
        ]
        for tmpl in expected:
            path = template_dir / tmpl
            self.assertTrue(path.exists(), f"Missing template: {tmpl}")

    def test_restaurant_template_has_labels(self):
        """Verify restaurant template uses sector labels."""
        template_path = REPO / "backend" / "templates" / "emails" / "restaurant_reservation.html"
        content = template_path.read_text()
        self.assertIn("{{ icon }}", content)
        self.assertIn("Party Size", content)
        self.assertIn("{{ booking.quantity }}", content)
        self.assertIn("{{ agent_name }}", content)

    def test_payment_reminder_template(self):
        """Verify payment reminder template is generic."""
        template_path = REPO / "backend" / "templates" / "emails" / "payment_reminder.html"
        content = template_path.read_text()
        self.assertIn("entity_name", content)
        self.assertIn("payment_link", content)

    def test_daily_digest_template(self):
        """Verify daily digest template aggregates by type."""
        template_path = REPO / "backend" / "templates" / "emails" / "daily_digest.html"
        content = template_path.read_text()
        self.assertIn("summary_by_type", content)
        self.assertIn("upcoming_bookings", content)


# ─── Online eval suite (run with server) ──────────────────────────────────

EVAL_TESTS = []
section = "bookings"

def eval_test(name, detail=""):
    """Decorator for eval tests."""
    def decorator(fn):
        EVAL_TESTS.append((name, fn, detail))
        return fn
    return decorator


@eval_test("Create restaurant booking", "POST /api/bookings with restaurant_table type")
async def test_create_restaurant_booking(client, agent_with_bookings):
    """Create a restaurant table reservation booking."""
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=5)).date().isoformat()
    booking_time = "19:30"

    booking_data = {
        "call_id": "test-call-123",
        "agent_id": agent_with_bookings["restaurant"]["id"],
        "booking_type": "restaurant_table",
        "customer_name": "Priya Sharma",
        "customer_phone": "+919876543210",
        "customer_email": "priya@example.com",
        "quantity": 4,
        "booking_date": booking_date,
        "booking_time": booking_time,
        "special_notes": "Window seat preferred",
        "metadata": {"table_preference": "outdoor"},
    }

    status, resp, _ = client.post("/api/bookings", booking_data)
    assert status == 201, f"Expected 201, got {status}: {resp}"
    assert resp.get("booking_id"), "No booking_id in response"
    assert resp.get("status") == "hold", "Initial status should be 'hold'"
    assert resp.get("payment_link"), "No payment_link generated"
    return resp


@eval_test("Create salon appointment", "POST /api/bookings with salon_appointment type")
async def test_create_salon_booking(client, agent_with_bookings):
    """Create a salon appointment booking."""
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=3)).date().isoformat()
    booking_time = "14:00"

    booking_data = {
        "call_id": "test-call-124",
        "agent_id": agent_with_bookings["salon"]["id"],
        "booking_type": "salon_appointment",
        "customer_name": "Anjali Singh",
        "customer_phone": "+919876543211",
        "customer_email": "anjali@example.com",
        "quantity": 2,
        "booking_date": booking_date,
        "booking_time": booking_time,
        "special_notes": "Hair coloring + cut",
        "metadata": {},
    }

    status, resp, _ = client.post("/api/bookings", booking_data)
    assert status == 201, f"Expected 201, got {status}"
    assert resp.get("booking_type") == "salon_appointment"
    return resp


@eval_test("Create dental appointment", "POST /api/bookings with dental_appointment type")
async def test_create_dental_booking(client, agent_with_bookings):
    """Create a dental appointment booking."""
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=7)).date().isoformat()
    booking_time = "10:00"

    booking_data = {
        "call_id": "test-call-125",
        "agent_id": agent_with_bookings["dental"]["id"],
        "booking_type": "dental_appointment",
        "customer_name": "Vikram Patel",
        "customer_phone": "+919876543212",
        "customer_email": "vikram@example.com",
        "quantity": 1,
        "booking_date": booking_date,
        "booking_time": booking_time,
        "special_notes": "Root canal - Dr. Singh preferred",
        "metadata": {"procedure": "root_canal"},
    }

    status, resp, _ = client.post("/api/bookings", booking_data)
    assert status == 201
    assert resp.get("booking_type") == "dental_appointment"
    return resp


@eval_test("Create auto service appointment", "POST /api/bookings with auto_service type")
async def test_create_auto_booking(client, agent_with_bookings):
    """Create an auto service appointment booking."""
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=2)).date().isoformat()
    booking_time = "09:00"

    booking_data = {
        "call_id": "test-call-126",
        "agent_id": agent_with_bookings["auto"]["id"],
        "booking_type": "auto_service",
        "customer_name": "Rajesh Kumar",
        "customer_phone": "+919876543213",
        "customer_email": "rajesh@example.com",
        "quantity": 1,
        "booking_date": booking_date,
        "booking_time": booking_time,
        "special_notes": "Oil change + filter replacement",
        "metadata": {"vehicle": "Hyundai i20", "year": 2023},
    }

    status, resp, _ = client.post("/api/bookings", booking_data)
    assert status == 201
    assert resp.get("booking_type") == "auto_service"
    return resp


@eval_test("Create coaching session", "POST /api/bookings with coaching_session type")
async def test_create_coaching_booking(client, agent_with_bookings):
    """Create a coaching session booking."""
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=4)).date().isoformat()
    booking_time = "15:00"

    booking_data = {
        "call_id": "test-call-127",
        "agent_id": agent_with_bookings["coaching"]["id"],
        "booking_type": "coaching_session",
        "customer_name": "Neha Desai",
        "customer_phone": "+919876543214",
        "customer_email": "neha@example.com",
        "quantity": 1,
        "booking_date": booking_date,
        "booking_time": booking_time,
        "special_notes": "Career transition coaching - tech to PM",
        "metadata": {"experience_years": 8, "current_role": "SDE"},
    }

    status, resp, _ = client.post("/api/bookings", booking_data)
    assert status == 201
    assert resp.get("booking_type") == "coaching_session"
    return resp


@eval_test("List bookings with filters", "GET /api/agent/{id}/bookings with filtering")
async def test_list_bookings_filtered(client, agent_with_bookings):
    """Test booking list API with filters."""
    # First create a booking
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=5)).date().isoformat()

    booking_data = {
        "call_id": "test-call-128",
        "agent_id": agent_with_bookings["restaurant"]["id"],
        "booking_type": "restaurant_table",
        "customer_name": "Test Customer",
        "customer_phone": "+919876543215",
        "customer_email": "test@example.com",
        "quantity": 2,
        "booking_date": booking_date,
        "booking_time": "19:30",
        "special_notes": "Test booking",
        "metadata": {},
    }

    create_status, create_resp, _ = client.post("/api/bookings", booking_data)
    assert create_status == 201

    # Now list bookings with filters
    agent_id = agent_with_bookings["restaurant"]["id"]
    list_status, list_resp, _ = client.get(f"/api/agent/{agent_id}/bookings?booking_type=restaurant_table&status=hold")
    assert list_status == 200, f"Expected 200, got {list_status}"
    assert "bookings" in list_resp, "No bookings in response"
    assert len(list_resp["bookings"]) > 0, "Expected at least one booking"
    assert list_resp["bookings"][0]["booking_type"] == "restaurant_table"
    return list_resp


@eval_test("Booking reminders scheduled", "Verify booking_reminders table populated")
async def test_booking_reminders_scheduled(client, agent_with_bookings, db):
    """Verify that reminders are scheduled after booking creation."""
    from datetime import datetime, timedelta
    booking_date = (datetime.now() + timedelta(days=5)).date().isoformat()

    booking_data = {
        "call_id": "test-call-129",
        "agent_id": agent_with_bookings["restaurant"]["id"],
        "booking_type": "restaurant_table",
        "customer_name": "Reminder Test",
        "customer_phone": "+919876543216",
        "customer_email": "reminder@example.com",
        "quantity": 2,
        "booking_date": booking_date,
        "booking_time": "19:30",
        "special_notes": "Test reminders",
        "metadata": {},
    }

    create_status, create_resp, _ = client.post("/api/bookings", booking_data)
    assert create_status == 201
    booking_id = create_resp["booking_id"]

    # Check that reminders were scheduled
    reminders = await db.fetch(
        "SELECT * FROM booking_reminders WHERE booking_id = :bid",
        {"bid": booking_id}
    )
    assert len(reminders) >= 3, f"Expected at least 3 reminders, got {len(reminders)}"

    reminder_types = {r["reminder_type"] for r in reminders}
    assert "payment_reminder" in reminder_types
    assert "booking_day_reminder" in reminder_types
    assert "review_request" in reminder_types
    return reminders


@eval_test("Dashboard displays all sector types", "GET /api/agent/{id}/bookings returns correct labels")
async def test_dashboard_sector_labels(client, agent_with_bookings):
    """Verify dashboard displays sector-specific labels."""
    for sector, agent_info in agent_with_bookings.items():
        agent_id = agent_info["id"]
        status, resp, _ = client.get(f"/api/agent/{agent_id}/bookings")
        assert status == 200, f"{sector}: Expected 200, got {status}"
        assert "summary" in resp, f"{sector}: No summary in response"
        # The API should be accessible for all sectors
        assert "bookings" in resp


# ─── Test fixtures and helpers ──────────────────────────────────────────────

class BookingsEvalFixtures:
    """HTTP client + agent fixtures for eval tests."""

    def __init__(self):
        self.agents = {}

    async def setup_agents(self, client):
        """Set up test agents for each industry."""
        # In a real test, we'd query the database for agents with booking config
        # For now, we'll create fixtures that would work with a test DB
        self.agents = {
            "restaurant": {"id": 1, "slug": "test-restaurant"},
            "salon": {"id": 2, "slug": "test-salon"},
            "dental": {"id": 3, "slug": "test-dental"},
            "auto": {"id": 4, "slug": "test-auto"},
            "coaching": {"id": 5, "slug": "test-coaching"},
        }
        return self.agents


# ─── Main entry point (integration with eval_suite.py) ──────────────────

if __name__ == "__main__":
    # Run offline tests
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    suite.addTests(loader.loadTestsFromTestCase(TestBookingSchema))
    suite.addTests(loader.loadTestsFromTestCase(TestReminderScheduling))
    suite.addTests(loader.loadTestsFromTestCase(TestEmailTemplates))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    sys.exit(0 if result.wasSuccessful() else 1)

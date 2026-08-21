"""Add generic booking system (multi-sector).

Revision ID: 0037_bookings_system
Revises: 0035_post_call_customer_email
Create Date: 2026-08-21 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0037_bookings_system'
down_revision = '0035_post_call_customer_email'
branch_labels = None
depends_on = None


def upgrade():
    # Create bookings table (generic, multi-sector)
    op.create_table(
        'bookings',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.func.gen_random_uuid()),
        sa.Column('call_id', sa.BigInteger(), nullable=True),
        sa.Column('agent_id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),

        # Booking type (restaurant_table, salon_appointment, dental_appointment, auto_service, coaching_session)
        sa.Column('booking_type', sa.VARCHAR(50), nullable=False),

        # Customer info
        sa.Column('customer_name', sa.VARCHAR(255), nullable=False),
        sa.Column('customer_phone', sa.VARCHAR(20), nullable=False),
        sa.Column('customer_email', sa.VARCHAR(255), nullable=True),

        # Core booking fields (generic across sectors)
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('booking_date', sa.Date(), nullable=False),
        sa.Column('booking_time', sa.Time(), nullable=False),
        sa.Column('duration_minutes', sa.Integer(), nullable=True),
        sa.Column('special_notes', sa.Text(), nullable=True),

        # Booking lifecycle
        sa.Column('status', sa.VARCHAR(50), nullable=False, server_default='hold'),
        sa.Column('held_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('held_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),

        # SMS & Payment tracking
        sa.Column('sms_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sms_delivery_status', sa.VARCHAR(50), nullable=True),
        sa.Column('sms_link_clicked_at', sa.DateTime(timezone=True), nullable=True),

        sa.Column('payment_link_id', sa.VARCHAR(255), nullable=True),
        sa.Column('payment_link_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payment_link_visited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('card_details_entered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payment_status', sa.VARCHAR(50), nullable=False, server_default='pending'),

        # Email tracking
        sa.Column('summary_email_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('summary_email_to', sa.VARCHAR(255), nullable=True),
        sa.Column('summary_email_cc', sa.VARCHAR(255), nullable=True),

        # Sector-specific metadata
        sa.Column('metadata', postgresql.JSONB(), nullable=False, server_default='{}'),

        # External system sync (Seven Rooms, etc.)
        sa.Column('external_booking_id', sa.VARCHAR(255), nullable=True),
        sa.Column('external_system', sa.VARCHAR(50), nullable=True),
        sa.Column('external_synced_at', sa.DateTime(timezone=True), nullable=True),

        # Audit
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('payment_link_id'),
    )

    # Create indexes
    op.create_index('ix_bookings_agent_id_booking_date', 'bookings', ['agent_id', 'booking_date'])
    op.create_index('ix_bookings_booking_type', 'bookings', ['booking_type'])
    op.create_index('ix_bookings_customer_phone', 'bookings', ['customer_phone'])
    op.create_index('ix_bookings_status', 'bookings', ['status'])
    op.create_index('ix_bookings_payment_status', 'bookings', ['payment_status'])
    op.create_index('ix_bookings_held_until', 'bookings', ['held_until'])

    # Create booking_reminders table
    op.create_table(
        'booking_reminders',
        sa.Column('id', sa.BigInteger(), sa.Sequence('booking_reminders_id_seq'), nullable=False),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reminder_type', sa.VARCHAR(50), nullable=False),
        sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.VARCHAR(50), nullable=False, server_default='pending'),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # Create index
    op.create_index('ix_booking_reminders_scheduled_for_status',
                   'booking_reminders',
                   ['scheduled_for', 'status'])


def downgrade():
    op.drop_index('ix_booking_reminders_scheduled_for_status', table_name='booking_reminders')
    op.drop_table('booking_reminders')

    op.drop_index('ix_bookings_held_until', table_name='bookings')
    op.drop_index('ix_bookings_payment_status', table_name='bookings')
    op.drop_index('ix_bookings_status', table_name='bookings')
    op.drop_index('ix_bookings_customer_phone', table_name='bookings')
    op.drop_index('ix_bookings_booking_type', table_name='bookings')
    op.drop_index('ix_bookings_agent_id_booking_date', table_name='bookings')
    op.drop_table('bookings')

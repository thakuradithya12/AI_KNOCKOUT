"""Phase 2: Participant identity, sessions, and auth events

Revision ID: 0002_phase2_identity_auth
Revises: 0001_initial_schema
Create Date: 2026-09-29 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_phase2_identity_auth'
down_revision: Union[str, None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create participants table
    op.create_table(
        'participants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('participant_id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('team_name', sa.String(length=100), nullable=True),
        sa.Column('access_key_hash', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='ACTIVE', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_participants_participant_id'), 'participants', ['participant_id'], unique=True)
    op.create_index(op.f('ix_participants_name'), 'participants', ['name'], unique=True)
    op.create_index(op.f('ix_participants_email'), 'participants', ['email'], unique=True)
    op.create_index(op.f('ix_participants_status'), 'participants', ['status'], unique=False)

    # 2. Create participant_sessions table
    op.create_table(
        'participant_sessions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('participant_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_ip', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['participant_id'], ['participants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_participant_sessions_participant_id'), 'participant_sessions', ['participant_id'], unique=False)
    op.create_index(op.f('ix_participant_sessions_token_hash'), 'participant_sessions', ['token_hash'], unique=True)
    op.create_index(op.f('ix_participant_sessions_expires_at'), 'participant_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_participant_sessions_revoked_at'), 'participant_sessions', ['revoked_at'], unique=False)

    # 3. Create auth_events table
    op.create_table(
        'auth_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('participant_id', sa.String(length=32), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_auth_events_participant_id'), 'auth_events', ['participant_id'], unique=False)
    op.create_index(op.f('ix_auth_events_event_type'), 'auth_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_auth_events_request_id'), 'auth_events', ['request_id'], unique=False)
    op.create_index(op.f('ix_auth_events_created_at'), 'auth_events', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_table('auth_events')
    op.drop_table('participant_sessions')
    op.drop_table('participants')

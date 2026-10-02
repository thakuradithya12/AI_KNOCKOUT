"""Authentication and participant identity service.

Manages:
- Secure participant registration with scrypt credential hashing
- Constant-time credential authentication and session token issuance
- Session verification and revocation (logout)
- Audit logging for all identity and security operations
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.security import (
    async_hash_access_key,
    async_verify_access_key,
    async_verify_dummy_hash,
    generate_access_key,
    generate_participant_id,
    generate_session_token,
    hash_token,
)
from app.models.participant import (
    AuthEvent,
    AuthEventType,
    Participant,
    ParticipantSession,
    ParticipantStatus,
)
from app.schemas.participant import ParticipantRegisterRequest

logger = logging.getLogger("ai-knockout.auth")


class AuthService:
    """Service handling participant identity and session lifecycle."""

    @staticmethod
    async def record_audit_event(
        db: AsyncSession,
        event_type: AuthEventType,
        participant_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        details: str | None = None,
    ) -> None:
        """Record an append-only security audit event.

        Best-effort persistence: will not break user flow if logging fails,
        but logs errors immediately.
        """
        try:
            event = AuthEvent(
                participant_id=participant_id,
                event_type=event_type,
                ip_address=ip_address,
                user_agent=user_agent[:255] if user_agent else None,
                request_id=request_id,
                details=details,
                created_at=datetime.now(timezone.utc),
            )
            db.add(event)
            await db.flush()
        except Exception as e:
            logger.error(f"Failed to record audit event {event_type.value}: {e}")

    @classmethod
    async def register_participant(
        cls,
        db: AsyncSession,
        req: ParticipantRegisterRequest,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> tuple[Participant, str]:
        """Register a new participant or team.

        Returns:
            Tuple of (Participant, plaintext_access_key)
        """
        # 1. Check name uniqueness
        stmt = select(Participant.id).where(Participant.name == req.name)
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            await cls.record_audit_event(
                db,
                AuthEventType.REGISTRATION_FAILURE,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                details="Participant name already exists",
            )
            await db.commit()
            raise ValueError("A participant or team with this name is already registered.")

        # 2. Check email uniqueness if provided
        if req.email:
            stmt = select(Participant.id).where(Participant.email == req.email)
            existing_email = (await db.execute(stmt)).scalar_one_or_none()
            if existing_email:
                await cls.record_audit_event(
                    db,
                    AuthEventType.REGISTRATION_FAILURE,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    request_id=request_id,
                    details="Participant email already exists",
                )
                await db.commit()
                raise ValueError("This email address is already registered.")

        # 3. Generate credentials
        participant_id = generate_participant_id()
        raw_access_key = generate_access_key()
        access_key_hash = await async_hash_access_key(raw_access_key)

        # 4. Create participant record with transactional constraint protection
        participant = Participant(
            participant_id=participant_id,
            name=req.name,
            email=req.email,
            team_name=req.team_name,
            access_key_hash=access_key_hash,
            status=ParticipantStatus.ACTIVE,
        )
        try:
            db.add(participant)
            await db.flush()

            # 5. Record audit event
            await cls.record_audit_event(
                db,
                AuthEventType.REGISTRATION_SUCCESS,
                participant_id=participant_id,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                details="Participant registered successfully",
            )

            await db.commit()
            await db.refresh(participant)
        except IntegrityError as exc:
            await db.rollback()
            await cls.record_audit_event(
                db,
                AuthEventType.REGISTRATION_FAILURE,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                details="Duplicate identity violation at database constraint boundary",
            )
            await db.commit()
            raise ValueError(
                "A participant or team with this name or email is already registered."
            ) from exc

        return participant, raw_access_key

    @classmethod
    async def authenticate_participant(
        cls,
        db: AsyncSession,
        participant_id: str,
        access_key: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> tuple[Participant, str, int]:
        """Authenticate participant using Participant ID and Access Key.

        Returns:
            Tuple of (Participant, bearer_session_token, expires_in_seconds)
        """
        settings = get_settings()

        # 1. Fetch participant record
        stmt = select(Participant).where(Participant.participant_id == participant_id)
        participant = (await db.execute(stmt)).scalar_one_or_none()

        # 2. Timing attack protection: equalise execution time if user not found
        if participant is None:
            await async_verify_dummy_hash(access_key)
            await cls.record_audit_event(
                db,
                AuthEventType.LOGIN_FAILURE,
                participant_id=participant_id,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                details="Invalid credentials",
            )
            await db.commit()
            raise ValueError("Invalid credentials.")

        # 3. Verify status
        if participant.status != ParticipantStatus.ACTIVE:
            await cls.record_audit_event(
                db,
                AuthEventType.LOGIN_FAILURE,
                participant_id=participant_id,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                details=f"Login rejected: participant status is {participant.status.value}",
            )
            await db.commit()
            raise ValueError("Participant account is suspended or revoked.")

        # 4. Verify Access Key
        valid = await async_verify_access_key(access_key, participant.access_key_hash)
        if not valid:
            await cls.record_audit_event(
                db,
                AuthEventType.LOGIN_FAILURE,
                participant_id=participant_id,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                details="Invalid credentials",
            )
            await db.commit()
            raise ValueError("Invalid credentials.")

        # 5. Issue session bearer token
        raw_token = generate_session_token()
        token_hash = hash_token(raw_token)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.session_expire_minutes)

        session = ParticipantSession(
            participant_id=participant.id,
            token_hash=token_hash,
            expires_at=expires_at,
            created_ip=ip_address,
            user_agent=user_agent[:255] if user_agent else None,
        )
        db.add(session)

        # 6. Record audit event
        await cls.record_audit_event(
            db,
            AuthEventType.LOGIN_SUCCESS,
            participant_id=participant_id,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            details="Participant logged in successfully",
        )

        await db.commit()

        expires_in = settings.session_expire_minutes * 60
        return participant, raw_token, expires_in

    @classmethod
    async def get_participant_by_token(
        cls,
        db: AsyncSession,
        token: str,
    ) -> Participant | None:
        """Validate bearer token and return the associated active participant."""
        token_hash = hash_token(token)
        now = datetime.now(timezone.utc)

        stmt = (
            select(ParticipantSession)
            .options(selectinload(ParticipantSession.participant))
            .where(
                ParticipantSession.token_hash == token_hash,
                ParticipantSession.revoked_at.is_(None),
                ParticipantSession.expires_at > now,
            )
        )
        session = (await db.execute(stmt)).scalar_one_or_none()
        if not session or not session.participant:
            return None

        # Ensure participant itself is still ACTIVE
        if session.participant.status != ParticipantStatus.ACTIVE:
            return None

        return session.participant

    @classmethod
    async def revoke_token(
        cls,
        db: AsyncSession,
        token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> bool:
        """Revoke a bearer session token (logout)."""
        token_hash = hash_token(token)
        stmt = (
            select(ParticipantSession)
            .options(selectinload(ParticipantSession.participant))
            .where(ParticipantSession.token_hash == token_hash)
        )
        session = (await db.execute(stmt)).scalar_one_or_none()
        if not session:
            return False

        session.revoked_at = datetime.now(timezone.utc)
        part_id = session.participant.participant_id if session.participant else None

        await cls.record_audit_event(
            db,
            AuthEventType.LOGOUT,
            participant_id=part_id,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            details="Session token revoked successfully",
        )
        await db.commit()
        return True

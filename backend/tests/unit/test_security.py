"""Unit tests for Phase 2 cryptographic primitives and security helpers."""

from __future__ import annotations

import pytest

from app.core.security import (
    async_hash_access_key,
    async_verify_access_key,
    async_verify_dummy_hash,
    generate_access_key,
    generate_participant_id,
    generate_session_token,
    hash_access_key,
    hash_token,
    verify_access_key,
)


class TestSecurityGenerators:
    """Test identifier and token generation."""

    def test_generate_participant_id(self):
        """Participant ID must have 'part_' prefix and 16 hex chars."""
        pid1 = generate_participant_id()
        pid2 = generate_participant_id()

        assert pid1.startswith("part_")
        assert len(pid1) == 21  # 5 ('part_') + 16 hex chars
        assert pid1 != pid2

    def test_generate_access_key(self):
        """Access key must have 'ak_' prefix and sufficient entropy."""
        k1 = generate_access_key()
        k2 = generate_access_key()

        assert k1.startswith("ak_")
        assert len(k1) > 40
        assert k1 != k2

    def test_generate_session_token(self):
        """Session token must have 'aik_sess_' prefix."""
        t1 = generate_session_token()
        t2 = generate_session_token()

        assert t1.startswith("aik_sess_")
        assert len(t1) > 40
        assert t1 != t2

    def test_hash_token_deterministic(self):
        """Token hashing must be deterministic SHA-256."""
        token = "aik_sess_sample_test_token"
        h1 = hash_token(token)
        h2 = hash_token(token)

        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex length


class TestScryptHashing:
    """Test scrypt password-style credential hashing and verification."""

    def test_scrypt_hash_and_verify_success(self):
        """Valid access key must verify against its scrypt hash."""
        raw_key = generate_access_key()
        hashed = hash_access_key(raw_key)

        assert hashed.startswith("$scrypt$n=16384,r=8,p=1$")
        assert verify_access_key(raw_key, hashed) is True

    def test_scrypt_verify_fails_for_wrong_key(self):
        """Wrong access key must return False."""
        raw_key = generate_access_key()
        hashed = hash_access_key(raw_key)

        assert verify_access_key("wrong_key", hashed) is False
        assert verify_access_key(raw_key + "_tampered", hashed) is False

    def test_scrypt_uses_unique_salts(self):
        """Two hashes of the same key must produce different outputs."""
        raw_key = "ak_identical_test_key_12345"
        h1 = hash_access_key(raw_key)
        h2 = hash_access_key(raw_key)

        assert h1 != h2
        assert verify_access_key(raw_key, h1) is True
        assert verify_access_key(raw_key, h2) is True

    def test_verify_malformed_hash(self):
        """Malformed hash string must safely return False without raising."""
        assert verify_access_key("any_key", "invalid_hash_string") is False
        assert verify_access_key("any_key", "") is False

    @pytest.mark.asyncio
    async def test_async_scrypt_helpers(self):
        """Async wrappers must execute without error."""
        raw_key = generate_access_key()
        hashed = await async_hash_access_key(raw_key)
        assert await async_verify_access_key(raw_key, hashed) is True
        assert await async_verify_access_key("wrong", hashed) is False

    @pytest.mark.asyncio
    async def test_dummy_hash_execution(self):
        """Dummy hash verification must execute without error."""
        await async_verify_dummy_hash("random_key")

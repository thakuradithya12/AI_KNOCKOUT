"""Cryptographic primitives and credential management for Phase 2.

Provides:
- Secure generation of Participant IDs, Access Keys, and Session Tokens
- Password-style credential hashing using RFC 7914 scrypt
- Constant-time verification to prevent timing attacks
- Offloading CPU-intensive hashing to threadpool (async-safe)
- SHA-256 hashing for fast, safe database lookup of session tokens
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets

# Scrypt configuration (OWASP recommended parameters)
SCRYPT_N = 16384  # CPU/memory cost
SCRYPT_R = 8  # Block size
SCRYPT_P = 1  # Parallelization
SCRYPT_DKLEN = 32  # Output key length
SALT_LENGTH = 16  # 128-bit salt

# Precomputed dummy hash for timing-attack equalization during authentication failures
_DUMMY_SALT = b"\x00" * SALT_LENGTH
_DUMMY_HASH = hashlib.scrypt(
    b"dummy_timing_equalization_key",
    salt=_DUMMY_SALT,
    n=SCRYPT_N,
    r=SCRYPT_R,
    p=SCRYPT_P,
    dklen=SCRYPT_DKLEN,
)
_DUMMY_STORED_HASH = (
    f"$scrypt$n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}${_DUMMY_SALT.hex()}${_DUMMY_HASH.hex()}"
)


def generate_participant_id() -> str:
    """Generate an opaque, server-authoritative, immutable Participant ID.

    Format: 'part_' followed by 16 random hexadecimal characters (64 bits of entropy).
    """
    return f"part_{secrets.token_hex(8)}"


def generate_access_key() -> str:
    """Generate a high-entropy secret Access Key for participant authentication.

    Format: 'ak_' followed by 43 URL-safe base64 characters (256 bits of entropy).
    """
    return f"ak_{secrets.token_urlsafe(32)}"


def generate_session_token() -> str:
    """Generate an opaque, random bearer token for participant sessions.

    Format: 'aik_sess_' followed by 43 URL-safe base64 characters.
    """
    return f"aik_sess_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """Compute a deterministic SHA-256 hash of an authentication bearer token.

    Used to query sessions in PostgreSQL without storing plaintext tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_access_key(access_key: str) -> str:
    """Hash an Access Key using RFC 7914 scrypt with a unique random salt."""
    salt = secrets.token_bytes(SALT_LENGTH)
    key_hash = hashlib.scrypt(
        access_key.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"$scrypt$n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}${salt.hex()}${key_hash.hex()}"


def _parse_scrypt_hash(stored_hash: str) -> tuple[int, int, int, bytes, bytes]:
    """Parse components from stored scrypt hash string."""
    parts = stored_hash.split("$")
    # Expected format: '', 'scrypt', 'n=16384,r=8,p=1', '<salt_hex>', '<hash_hex>'
    if len(parts) != 5 or parts[1] != "scrypt":
        raise ValueError("Invalid hash format.")

    params = {}
    for param in parts[2].split(","):
        k, v = param.split("=")
        params[k] = int(v)

    salt = bytes.fromhex(parts[3])
    expected_hash = bytes.fromhex(parts[4])
    return params["n"], params["r"], params["p"], salt, expected_hash


def verify_access_key(access_key: str, stored_hash: str) -> bool:
    """Verify an Access Key against a stored scrypt hash in constant time."""
    try:
        n, r, p, salt, expected_hash = _parse_scrypt_hash(stored_hash)
        computed_hash = hashlib.scrypt(
            access_key.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected_hash),
        )
        return hmac.compare_digest(computed_hash, expected_hash)
    except Exception:
        return False


def verify_dummy_hash(access_key: str = "dummy") -> None:
    """Perform dummy hash computation to prevent timing attacks on non-existent accounts."""
    verify_access_key(access_key, _DUMMY_STORED_HASH)


async def async_hash_access_key(access_key: str) -> str:
    """Async wrapper: offloads CPU-intensive scrypt hashing to threadpool."""
    return await asyncio.to_thread(hash_access_key, access_key)


async def async_verify_access_key(access_key: str, stored_hash: str) -> bool:
    """Async wrapper: offloads CPU-intensive scrypt verification to threadpool."""
    return await asyncio.to_thread(verify_access_key, access_key, stored_hash)


async def async_verify_dummy_hash(access_key: str = "dummy") -> None:
    """Async wrapper: offloads dummy hash verification to threadpool."""
    await asyncio.to_thread(verify_dummy_hash, access_key)

"""Passwords and tokens. Pure functions: no FastAPI, no database. Design: docs/adr/013-authentication.md."""
import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

ALGORITHM = "HS256"
_hasher = PasswordHasher()      # argon2id with the library's defaults (OWASP-recommended family)
# Verified against when the email is unknown, so "no such user" costs the same time as "wrong password".
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


def make_access_token(user_id: UUID, role: str, organization_id: UUID | None, now: datetime, minutes: int, secret: str) -> str:
    claims = {
        "sub": str(user_id),
        "role": role,
        "org": str(organization_id) if organization_id else None,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def read_access_token(token: str, secret: str) -> dict | None:
    """Claims, or None for anything wrong: bad signature, expired, wrong algorithm, missing claims.

    algorithms=[ALGORITHM] is the line that blocks the classic `alg: none` and algorithm-confusion attacks."""
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["sub", "role", "exp"]})
    except jwt.PyJWTError:
        return None


def new_refresh_token() -> str:
    """Opaque and random: it means nothing by itself, the database says what it is worth."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """SHA-256 is enough here (unlike passwords): the token is 256 random bits, nothing to brute-force."""
    return hashlib.sha256(token.encode()).hexdigest()


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"     # base32 without I, L, O, U: nothing to misread on a paper form


def short_code(n: int = 8) -> str:
    """A request code a ward writes by hand. 32^8 = 1.1e12 codes; uniqueness is the database's UNIQUE (migration 0017)."""
    return "".join(secrets.choice(CROCKFORD) for _ in range(n))


if __name__ == "__main__":     # python security.py: a runnable self-check
    assert len(short_code()) == 8 and set(short_code(1000)) <= set(CROCKFORD)
    from datetime import timezone
    from uuid import uuid4

    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple") and not verify_password(h, "wrong")
    assert not verify_password(None, "anything")
    now, uid, key = datetime.now(timezone.utc), uuid4(), "k" * 32
    claims = read_access_token(make_access_token(uid, "donor", None, now, 15, key), key)
    assert claims["sub"] == str(uid) and claims["role"] == "donor"
    assert read_access_token(make_access_token(uid, "donor", None, now, 15, key), "x" * 32) is None
    assert read_access_token(make_access_token(uid, "donor", None, now - timedelta(hours=1), 15, key), key) is None
    print("security self-check ok")

"""Password hashing, session tokens and CSRF.

Design choices worth stating, because they are the ones that get argued about:

* **argon2id**, not bcrypt or SHA. Memory-hard, so GPU cracking of a stolen
  database is expensive.
* **Opaque server-side sessions**, not JWTs in the browser. A JWT cannot be
  revoked before it expires; a row in a table can be deleted. Logout should
  actually log you out.
* The session id is **hashed before storage**, exactly like a password. If the
  sessions table leaks, the tokens in it are not usable.
* **Double-submit CSRF**: the token is in a cookie *and* must be echoed in a
  header. An attacker's site can make the browser send the cookie but cannot
  read it to set the header.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
CSRF_COOKIE = "gf_csrf"
CSRF_HEADER = "X-CSRF-Token"


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when argon2 parameters have been raised since this hash was made."""
    return _hasher.check_needs_rehash(password_hash)


# A real argon2id hash used to burn the same CPU time when an email does not
# exist. Without it, "unknown email" returns measurably faster than "wrong
# password", which enumerates accounts.
_DUMMY_HASH = _hasher.hash("graphforge-timing-equaliser")


def waste_time_like_a_real_verify() -> None:
    verify_password("not-the-password", _DUMMY_HASH)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Store this, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def csrf_ok(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)

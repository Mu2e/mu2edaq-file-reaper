"""API token generation, hashing and verification.  Pure, plus a tiny TTL cache.

Tokens are ``rpr_`` + 32 random URL-safe bytes (256 bits of entropy).  The
database stores a PBKDF2-SHA256 hash in the same ``pbkdf2$iter$salt$hex`` format
mu2edaq-snapshot-viewer uses, plus the first 12 characters as a *prefix* so a
presented token can be looked up by an indexed column and then verified against
the (normally single) candidate with a constant-time compare.  Iterations are
modest because the secret is already high-entropy; stretching is defence in
depth, not the primary protection.
"""

import hashlib
import hmac
import secrets
import time
from typing import Dict, FrozenSet, Optional, Tuple

TOKEN_PREFIX_TAG = "rpr_"
PREFIX_LEN = 12
PBKDF2_ITERATIONS = 20_000

SCOPES = ("read", "operate", "admin")
#: Which scopes satisfy a requirement: admin implies operate implies read.
_IMPLIES = {"read": {"read", "operate", "admin"},
            "operate": {"operate", "admin"},
            "admin": {"admin"}}


def generate_token() -> str:
    return TOKEN_PREFIX_TAG + secrets.token_urlsafe(32)


def token_prefix(token: str) -> str:
    return token[:PREFIX_LEN]


def hash_token(token: str, salt: Optional[str] = None,
               iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", token.encode(), salt.encode(), iterations).hex()
    return "pbkdf2$%d$%s$%s" % (iterations, salt, digest)


def verify_token(token: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2":
            return False
        iterations = int(iters)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", token.encode(), salt.encode(), iterations).hex()
    return hmac.compare_digest(candidate, digest)


def scope_allows(granted: FrozenSet[str], required: str) -> bool:
    """True if any granted scope satisfies *required* (admin > operate > read)."""
    return bool(granted & _IMPLIES.get(required, {required}))


def normalise_scopes(scopes) -> FrozenSet[str]:
    if isinstance(scopes, str):
        scopes = scopes.split(",")
    out = {s.strip() for s in scopes if s and s.strip()}
    bad = out - set(SCOPES)
    if bad:
        raise ValueError(f"unknown scope(s): {', '.join(sorted(bad))}")
    return frozenset(out) or frozenset({"read"})


class TokenCache:
    """Verified token -> (token_id, name, scopes) with a TTL, so the PBKDF2 cost
    is paid once per token per *ttl* seconds rather than per request."""

    def __init__(self, ttl: float = 300.0, clock=time.time) -> None:
        self.ttl = ttl
        self._clock = clock
        self._items: Dict[str, Tuple[float, Tuple[int, str, FrozenSet[str]]]] = {}

    def get(self, token: str) -> Optional[Tuple[int, str, FrozenSet[str]]]:
        item = self._items.get(token)
        if item is None:
            return None
        exp, value = item
        if exp < self._clock():
            self._items.pop(token, None)
            return None
        return value

    def put(self, token: str, value: Tuple[int, str, FrozenSet[str]]) -> None:
        self._items[token] = (self._clock() + self.ttl, value)

    def invalidate_id(self, token_id: int) -> None:
        for key, (_exp, value) in list(self._items.items()):
            if value[0] == token_id:
                self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()

"""Authentication for the web UI and the API.

Two independent mechanisms:

* **Bearer tokens** (``Authorization: Bearer rpr_...``) for the API.  Each token
  carries scopes ``read`` < ``operate`` < ``admin``.  Looked up by prefix,
  verified with PBKDF2 + constant-time compare, cached for a few minutes.
* **Admin session** for the browser.  ``POST /login`` with the configured
  ``reaper.admin_token`` sets a signed session cookie; mutating requests made
  with that session must also carry the CSRF token the page embeds.

Read-only endpoints are open when ``api.open_read`` is true (the default, like
every other control-room dashboard).  Mutating endpoints always need one of the
two mechanisms.  A request may also present the admin secret directly as
``X-Admin-Token`` — that is how the CLI mints its first API token.
"""

import hmac
import logging
import secrets
from functools import wraps
from typing import Callable, FrozenSet, Optional, Tuple

from flask import current_app, g, jsonify, request, session

from ..auth import TokenCache, scope_allows, token_prefix, verify_token
from ..settings import get_settings

log = logging.getLogger("reaper.web.auth")

ADMIN_SCOPES = frozenset({"admin"})


class Principal:
    def __init__(self, kind: str, name: str, scopes: FrozenSet[str], token_id: Optional[int] = None):
        self.kind = kind            # token | session | admin_header | anonymous
        self.name = name
        self.scopes = scopes
        self.token_id = token_id

    @property
    def actor(self) -> str:
        if self.kind == "token":
            return f"token:{self.name}"
        if self.kind in ("session", "admin_header"):
            return f"ui:{self.name}"
        return "anonymous"

    def allows(self, scope: str) -> bool:
        return scope_allows(self.scopes, scope)


ANONYMOUS = Principal("anonymous", "anonymous", frozenset())


def bearer_token() -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip() or None
    return None


def _admin_secret_ok(presented: Optional[str]) -> bool:
    secret = get_settings().admin_token
    return bool(secret and presented and hmac.compare_digest(str(presented), str(secret)))


def resolve_principal() -> Principal:
    """Who is making this request?  Cached on ``g`` for the request."""
    cached = getattr(g, "principal", None)
    if cached is not None:
        return cached
    principal = ANONYMOUS
    token = bearer_token()
    if token:
        principal = _principal_from_token(token) or ANONYMOUS
    elif _admin_secret_ok(request.headers.get("X-Admin-Token")):
        principal = Principal("admin_header", "admin", ADMIN_SCOPES)
    elif session.get("admin"):
        principal = Principal("session", "admin", ADMIN_SCOPES)
    g.principal = principal
    return principal


def _principal_from_token(token: str) -> Optional[Principal]:
    cache: TokenCache = current_app.extensions["reaper"]["token_cache"]
    repo = current_app.extensions["reaper"]["tokens"]
    hit = cache.get(token)
    if hit is not None:
        token_id, name, scopes = hit
        return Principal("token", name, scopes, token_id)
    for cand in repo.candidates(token_prefix(token)):
        if verify_token(token, cand["token_hash"]):
            scopes = frozenset(cand["scopes"])
            cache.put(token, (cand["id"], cand["name"], scopes))
            try:
                repo.touch(cand["id"], request.remote_addr)
            except Exception as exc:
                log.debug("token touch failed: %s", exc)
            return Principal("token", cand["name"], scopes, cand["id"])
    return None


def csrf_ok() -> bool:
    expected = session.get("csrf")
    presented = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return bool(expected and presented and hmac.compare_digest(expected, presented))


def _deny(status: int, message: str):
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": message}), status
    return (message, status)


def require_scope(scope: str, allow_open_read: bool = False) -> Callable:
    """Decorator: the caller needs *scope*.  For GET endpoints pass
    ``allow_open_read=True`` so ``api.open_read`` can waive authentication."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            settings = get_settings()
            if allow_open_read and request.method == "GET" and settings.api_open_read:
                resolve_principal()
                return fn(*args, **kwargs)
            principal = resolve_principal()
            if principal.kind == "anonymous":
                return _deny(401, "authentication required (bearer token or admin login)")
            if principal.kind == "session" and request.method not in ("GET", "HEAD", "OPTIONS") and not csrf_ok():
                return _deny(403, "missing or invalid CSRF token")
            if not principal.allows(scope):
                return _deny(403, f"scope '{scope}' required")
            return fn(*args, **kwargs)
        return wrapper
    return deco


def login_admin(presented: str) -> bool:
    if not _admin_secret_ok(presented):
        return False
    session["admin"] = True
    session["csrf"] = secrets.token_urlsafe(24)
    return True


def logout_admin() -> None:
    session.pop("admin", None)
    session.pop("csrf", None)


def is_admin_session() -> bool:
    return bool(session.get("admin"))


def csrf_token() -> str:
    return session.get("csrf", "")

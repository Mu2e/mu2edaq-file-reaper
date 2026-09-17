"""Thin stdlib HTTP client for the mu2edaq-file-reaper REST API (``/api/v1``).

Every method returns the parsed JSON body.  HTTP errors surface as
:class:`ReaperError` (status + the server's ``error`` message); socket-level
failures as :class:`ReaperConnectionError`.  Authentication is a bearer token
(``Authorization: Bearer rpr_...``); the admin secret may be presented as
``X-Admin-Token`` instead, which is how the very first token is minted.
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Union

from . import __version__

API_PREFIX = "/api/v1"


class ReaperError(Exception):
    """The server answered with an HTTP error status."""

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class ReaperConnectionError(Exception):
    """The server could not be reached (DNS, refused, timeout, ...)."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"cannot reach {url}: {reason}")
        self.url = url
        self.reason = reason


def normalise_url(url: str) -> str:
    """Strip whitespace and trailing slashes; add ``http://`` when no scheme."""
    u = (url or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "http://" + u
    return u


def _clean_params(params: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (params or {}).items():
        if v is None or v is False:
            continue
        if v is True:
            out[k] = "1"
        elif isinstance(v, (list, tuple)):
            if v:
                out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def _clean_body(body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {k: v for k, v in (body or {}).items() if v is not None}


class ReaperClient:
    def __init__(self, base_url: str, token: Optional[str] = None,
                 admin_token: Optional[str] = None, timeout: float = 10.0) -> None:
        self.base_url = normalise_url(base_url)
        self.token = token or None
        self.admin_token = admin_token or None
        self.timeout = float(timeout)

    # ------------------------------------------------------------------ core --
    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json",
             "User-Agent": f"mu2edaq-reaper/{__version__}"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        elif self.admin_token:
            h["X-Admin-Token"] = self.admin_token
        return h

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + API_PREFIX + path
        q = _clean_params(params)
        if q:
            url += "?" + urllib.parse.urlencode(q)
        return url

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None, raw: bool = False) -> Any:
        url = self._url(path, params)
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(_clean_body(body)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw_body = b""
            try:
                raw_body = exc.read()
            except Exception:
                pass
            message, parsed = _error_message(raw_body, exc.reason)
            raise ReaperError(exc.code, message, parsed) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, Exception):
                reason = str(reason) or reason.__class__.__name__
            raise ReaperConnectionError(self.base_url, str(reason)) from None
        except (socket.timeout, TimeoutError):
            raise ReaperConnectionError(self.base_url, f"timed out after {self.timeout:g}s") from None
        except (ConnectionError, OSError) as exc:
            raise ReaperConnectionError(self.base_url, str(exc)) from None
        text = payload.decode("utf-8", errors="replace")
        if raw or not ("json" in ctype or text[:1] in "{["):
            return text
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("POST", path, body=body if body is not None else {})

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    # -------------------------------------------------------------- liveness --
    def health(self) -> Dict[str, Any]:
        return self.get("/health")

    def version(self) -> Dict[str, Any]:
        return self.get("/version")

    def whoami(self) -> Dict[str, Any]:
        return self.get("/whoami")

    # ----------------------------------------------------------------- areas --
    def state(self) -> Dict[str, Any]:
        return self.get("/state")

    def areas(self) -> Dict[str, Any]:
        return self.get("/areas")

    def area(self, name: str) -> Dict[str, Any]:
        return self.get(f"/areas/{urllib.parse.quote(name, safe='')}")

    def _area_action(self, name: str, verb: str, reason: Optional[str] = None,
                     **extra: Any) -> Dict[str, Any]:
        body = {"reason": reason}
        body.update(extra)
        return self.post(f"/areas/{urllib.parse.quote(name, safe='')}/{verb}", body)

    def pause(self, name: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return self._area_action(name, "pause", reason)

    def resume(self, name: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return self._area_action(name, "resume", reason)

    def rescan(self, name: str, reason: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
        return self._area_action(name, "rescan", reason, dry_run=dry_run or None)

    def disable(self, name: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return self._area_action(name, "disable", reason)

    def enable(self, name: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return self._area_action(name, "enable", reason)

    def dry_run(self, name: str) -> Dict[str, Any]:
        return self.post(f"/dry-run/{urllib.parse.quote(name, safe='')}", {})

    # ---------------------------------------------------------------- queues --
    def queues(self, area: Optional[str] = None, kind: Optional[str] = None,
               status: Optional[str] = None) -> Dict[str, Any]:
        return self.get("/queues", {"area": area, "kind": kind, "status": status})

    def queue_entry(self, entry_id: str) -> Dict[str, Any]:
        return self.get("/queues/" + urllib.parse.quote(entry_id.lstrip("/"), safe="/:"))

    # ------------------------------------------------------------ exclusions --
    def exclusions(self, area: Optional[str] = None, all: bool = False) -> Dict[str, Any]:
        return self.get("/exclusions", {"area": area, "all": bool(all)})

    def add_exclusion(self, pattern: str, area: Optional[str] = None, kind: Optional[str] = None,
                      reason: Optional[str] = None, expires_in: Optional[str] = None) -> Dict[str, Any]:
        return self.post("/exclusions", {"pattern": pattern, "area": area, "kind": kind,
                                         "reason": reason, "expires_in": expires_in})

    def remove_exclusion(self, exclusion_id: int) -> Dict[str, Any]:
        return self.delete(f"/exclusions/{int(exclusion_id)}")

    # --------------------------------------------------------------- history --
    def _history_params(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(filters)
        types = p.pop("types", None) or p.pop("type", None)
        if types:
            p["type"] = types
        if p.pop("asc", False):
            p["order"] = "asc"
        return p

    def history(self, **filters: Any) -> Dict[str, Any]:
        return self.get("/history", self._history_params(filters))

    def history_csv(self, **filters: Any) -> str:
        params = self._history_params(filters)
        params["format"] = "csv"
        return self.request("GET", "/history", params=params, raw=True)

    def history_types(self) -> Dict[str, Any]:
        return self.get("/history/types")

    # --------------------------------------------------------- notifications --
    def notifications(self, limit: Optional[int] = None) -> Dict[str, Any]:
        return self.get("/notifications", {"limit": limit})

    def test_notification(self, channel: Optional[str] = None,
                          severity: Optional[str] = None) -> Dict[str, Any]:
        return self.post("/notifications/test", {"channel": channel, "severity": severity})

    # ---------------------------------------------------------------- tokens --
    def tokens(self, all: bool = False) -> Dict[str, Any]:
        return self.get("/tokens", {"all": bool(all)})

    def create_token(self, name: str, scopes: Union[str, Iterable[str]],
                     expires_in: Optional[str] = None) -> Dict[str, Any]:
        if isinstance(scopes, str):
            scope_list: List[str] = [s.strip() for s in scopes.split(",") if s.strip()]
        else:
            scope_list = [str(s) for s in scopes]
        return self.post("/tokens", {"name": name, "scopes": scope_list, "expires_in": expires_in})

    def revoke_token(self, token_id: int) -> Dict[str, Any]:
        return self.delete(f"/tokens/{int(token_id)}")

    # ---------------------------------------------------------------- config --
    def config(self) -> Dict[str, Any]:
        return self.get("/config")


def _error_message(raw: bytes, fallback: Any) -> "tuple":
    """Pull ``error`` out of a JSON error body; fall back to the HTTP reason."""
    text = raw.decode("utf-8", errors="replace") if raw else ""
    parsed: Any = None
    if text.strip():
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed["error"]), parsed
    if text.strip() and parsed is None and len(text) < 300 and "<" not in text:
        return text.strip(), None
    return str(fallback or "request failed"), parsed

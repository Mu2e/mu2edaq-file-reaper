"""Notification channels.  Each ``send()`` raises on failure; the Notifier
records the outcome.  Optional third-party imports happen lazily so a missing
package only disables that one channel."""

import json
import smtplib
import socket
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from .ratelimit import severity_rank


@dataclass(frozen=True)
class Event:
    severity: str
    title:    str
    message:  str
    key:      str
    area:     Optional[str] = None
    meta:     Dict[str, str] = field(default_factory=dict)
    ts:       float = 0.0


class Channel:
    name = "base"

    def __init__(self, cfg: Dict[str, Any], label: str) -> None:
        self.cfg = cfg or {}
        self.label = label
        self.enabled = bool(self.cfg.get("enabled", False))
        self.min_severity = str(self.cfg.get("min_severity", "warning"))
        self.disabled_reason: Optional[str] = None

    def accepts(self, ev: Event) -> bool:
        return self.enabled and severity_rank(ev.severity) >= severity_rank(self.min_severity)

    def send(self, ev: Event) -> None:            # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "min_severity": self.min_severity,
                "disabled_reason": self.disabled_reason}


def _prefix(label: str, ev: Event) -> str:
    return f"[file-reaper {label}] {ev.title}"


# ---------------------------------------------------------------------------
class Mu2eNotifyChannel(Channel):
    """mu2edaq-phone-notification-system via the stdlib-only NotifyPublisher."""

    name = "mu2e_notify"

    def __init__(self, cfg, label):
        super().__init__(cfg, label)
        self._pub = None
        if self.enabled:
            try:
                from mu2edaq_notify import NotifyPublisher
                self._pub = NotifyPublisher(server_url=self.cfg.get("url") or None,
                                            token=self.cfg.get("token") or None,
                                            source=self.cfg.get("source") or "file-reaper",
                                            category=self.cfg.get("category") or "DAQ")
            except Exception as exc:
                self.enabled = False
                self.disabled_reason = f"mu2edaq_notify unavailable: {exc}"

    def send(self, ev: Event) -> None:
        meta = {k: str(v) for k, v in ev.meta.items()}
        meta.setdefault("instance", self.label)
        if ev.area:
            meta.setdefault("area", ev.area)
        ok = self._pub.publish(ev.severity, _prefix(self.label, ev), ev.message, meta=meta)
        if not ok:
            raise RuntimeError("NotifyPublisher reported delivery failure")


# ---------------------------------------------------------------------------
class DaqMessageChannel(Channel):
    """ZMQ PUSH to the mu2edaq-dashboard PULL socket (default port 5555)."""

    name = "daq_messages"
    LEVELS = {"critical": "error", "error": "error", "warning": "warning",
              "info": "normal", "debug": "debug"}

    def __init__(self, cfg, label):
        super().__init__(cfg, label)
        self._ctx = None
        self._sock = None
        if self.enabled:
            try:
                import zmq  # noqa: F401
            except Exception as exc:
                self.enabled = False
                self.disabled_reason = f"pyzmq unavailable: {exc}"

    def _socket(self):
        import zmq
        if self._sock is None:
            self._ctx = zmq.Context.instance()
            self._sock = self._ctx.socket(zmq.PUSH)
            self._sock.setsockopt(zmq.LINGER, 500)
            self._sock.setsockopt(zmq.SNDTIMEO, int(self.cfg.get("timeout_ms", 1000)))
            self._sock.setsockopt(zmq.SNDHWM, 100)
            self._sock.connect("tcp://%s:%d" % (self.cfg.get("host", "localhost"),
                                                int(self.cfg.get("port", 5555))))
        return self._sock

    def make_message(self, ev: Event) -> Dict[str, Any]:
        return {
            "subsystem":      str(self.cfg.get("subsystem", "DAQ")),
            "error_level":    self.LEVELS.get(ev.severity, "normal"),
            "event_category": str(self.cfg.get("event_category", "software")),
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "message":        f"{_prefix(self.label, ev)}: {ev.message}",
        }

    def send(self, ev: Event) -> None:
        import zmq
        try:
            self._socket().send_string(json.dumps(self.make_message(ev)))
        except zmq.ZMQError as exc:
            raise RuntimeError(f"zmq send failed: {exc}") from exc


# ---------------------------------------------------------------------------
class BigRedBoxChannel(Channel):
    """UDP broadcast to the control-room Big Red Box (default port 37020)."""

    name = "bigredbox"

    def __init__(self, cfg, label):
        super().__init__(cfg, label)
        self.min_severity = str(self.cfg.get("min_severity", "critical"))

    def payload(self, ev: Event) -> Dict[str, Any]:
        return {"system_id": str(self.cfg.get("system_id") or f"FILE-REAPER-{self.label}".upper()),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "message": f"{ev.title}: {ev.message}"}

    def send(self, ev: Event) -> None:
        try:
            from daq_alert import send_alert
            send_alert(self.payload(ev)["system_id"], self.payload(ev)["message"],
                       ip=self.cfg.get("ip", "255.255.255.255"), port=int(self.cfg.get("port", 37020)))
            return
        except ImportError:
            pass
        data = json.dumps(self.payload(ev)).encode()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(2.0)
            sock.sendto(data, (self.cfg.get("ip", "255.255.255.255"), int(self.cfg.get("port", 37020))))
        finally:
            sock.close()


# ---------------------------------------------------------------------------
class EmailChannel(Channel):
    name = "email"

    def __init__(self, cfg, label):
        super().__init__(cfg, label)
        self.min_severity = str(self.cfg.get("min_severity", "critical"))
        self.recipients: List[str] = list(self.cfg.get("to") or [])
        if self.enabled and not self.recipients:
            self.enabled = False
            self.disabled_reason = "no recipients configured (email.to)"

    def build(self, ev: Event) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[{ev.severity.upper()}] {_prefix(self.label, ev)}"
        msg["From"] = self.cfg.get("from", "mu2edaq@fnal.gov")
        msg["To"] = ", ".join(self.recipients)
        lines = [ev.message, "", f"instance: {self.label}"]
        if ev.area:
            lines.append(f"area: {ev.area}")
        for k, v in sorted(ev.meta.items()):
            lines.append(f"{k}: {v}")
        msg.set_content("\n".join(lines))
        return msg

    def send(self, ev: Event) -> None:
        msg = self.build(ev)
        if self.cfg.get("method", "smtp") == "mail" or self.cfg.get("use_mail_command"):
            subprocess.run(["mail", "-s", msg["Subject"]] + self.recipients,
                           input=msg.get_content(), text=True, check=True, timeout=30)
            return
        host = self.cfg.get("smtp_host", "localhost")
        port = int(self.cfg.get("smtp_port", 25))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if self.cfg.get("starttls"):
                smtp.starttls()
            user, pw = self.cfg.get("username"), self.cfg.get("password")
            if user and pw:
                smtp.login(user, pw)
            smtp.send_message(msg)


# ---------------------------------------------------------------------------
class SlackChannel(Channel):
    name = "slack"
    COLORS = {"debug": "#8E8E93", "info": "#2E86DE", "warning": "#F39C12",
              "error": "#E74C3C", "critical": "#8E44AD"}

    def __init__(self, cfg, label):
        super().__init__(cfg, label)
        self.webhook = self.cfg.get("webhook_url")
        if self.enabled and not self.webhook:
            self.enabled = False
            self.disabled_reason = "no webhook_url configured"

    def payload(self, ev: Event) -> Dict[str, Any]:
        fields = [{"title": "Instance", "value": self.label, "short": True},
                  {"title": "Severity", "value": ev.severity, "short": True}]
        if ev.area:
            fields.append({"title": "Area", "value": ev.area, "short": True})
        for k, v in sorted(ev.meta.items()):
            fields.append({"title": k, "value": str(v), "short": True})
        return {"text": f"[{ev.severity.upper()}] {_prefix(self.label, ev)}",
                "attachments": [{"color": self.COLORS.get(ev.severity, "#8E8E93"),
                                 "text": ev.message, "fields": fields}]}

    def send(self, ev: Event) -> None:
        req = urllib.request.Request(self.webhook, data=json.dumps(self.payload(ev)).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"slack webhook returned {resp.status}")


CHANNEL_CLASSES = {
    "mu2e_notify":  Mu2eNotifyChannel,
    "daq_messages": DaqMessageChannel,
    "bigredbox":    BigRedBoxChannel,
    "email":        EmailChannel,
    "slack":        SlackChannel,
}


def build_channels(notifications_cfg: Dict[str, Any], label: str) -> List[Channel]:
    channels = []
    for name, cls in CHANNEL_CLASSES.items():
        channels.append(cls(notifications_cfg.get(name) or {}, label))
    return channels

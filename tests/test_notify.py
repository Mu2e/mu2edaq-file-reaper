import json

from mu2edaq_file_reaper.notify import Notifier, build_notifier
from mu2edaq_file_reaper.notify.channels import (
    BigRedBoxChannel, Channel, DaqMessageChannel, EmailChannel, Event, SlackChannel,
)
from mu2edaq_file_reaper.notify.ratelimit import RateLimiter


class Recorder(Channel):
    def __init__(self, name, min_severity="info", fail=False):
        super().__init__({"enabled": True, "min_severity": min_severity}, "t")
        self.name = name
        self.sent = []
        self.fail = fail

    def send(self, ev):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(ev)


def ev(sev="warning", key="k", title="t"):
    return Event(severity=sev, title=title, message="m", key=key, area="raw", meta={"x": "1"}, ts=0)


def test_fanout_min_severity_dedup_and_logging(repos):
    a, b, c = Recorder("a", "info"), Recorder("b", "error"), Recorder("c", fail=True)
    disabled = Recorder("d")
    disabled.enabled = False
    n = Notifier([a, b, c, disabled], RateLimiter(window=100), log_repo=repos["notif_log"],
                 history=repos["history"], clock=lambda: 0.0)
    out = n.emit_sync(ev("warning"))
    assert out == {"a": "sent", "b": "below_min_severity", "c": "failed", "d": "disabled"}
    out2 = n.emit_sync(ev("warning"))
    assert out2["a"] == "suppressed_dup"
    out3 = n.emit_sync(ev("critical"))
    assert out3 == {"a": "sent", "b": "sent", "c": "failed", "d": "disabled"}
    assert n.sent_count == 3 and n.failed_count == 3
    log = repos["notif_log"].recent()
    assert {r["outcome"] for r in log} == {"sent", "failed", "suppressed_dup"}
    rows, total = repos["history"].query(event_types=["notification_sent"])
    assert total == 2 and rows[0]["detail"]["channels"]["b"] == "sent"


def test_worker_thread_delivers(repos):
    a = Recorder("a")
    n = Notifier([a], RateLimiter(window=0))
    n.start()
    n.emit("warning", "T", "M", key="k1", area="raw", meta={"n": 1})
    n.emit("warning", "T2", "M", key="k2")
    n.stop(timeout=5)
    assert sorted(e.title for e in a.sent) == ["T", "T2"]
    assert a.sent[0].meta == {"n": "1"} or a.sent[1].meta == {"n": "1"}


def test_build_notifier_disables_unconfigured_channels():
    n = build_notifier({"slack": {"enabled": True}, "email": {"enabled": True, "min_severity": "error"},
                        "bigredbox": {"enabled": True}}, "dl-01")
    d = {c["name"]: c for c in n.describe()}
    assert d["slack"]["enabled"] is False and "webhook" in d["slack"]["disabled_reason"]
    assert d["email"]["enabled"] is False and "recipients" in d["email"]["disabled_reason"]
    assert d["bigredbox"]["enabled"] is True and d["bigredbox"]["min_severity"] == "critical"
    assert d["mu2e_notify"]["enabled"] is False


def test_payload_shapes():
    daq = DaqMessageChannel({"enabled": False, "subsystem": "DAQ", "event_category": "software"}, "dl-01")
    msg = daq.make_message(ev("critical"))
    assert set(msg) == {"subsystem", "error_level", "event_category", "timestamp", "message"}
    assert msg["error_level"] == "error" and "[file-reaper dl-01] t" in msg["message"]
    assert DaqMessageChannel({}, "x").make_message(ev("info"))["error_level"] == "normal"
    brb = BigRedBoxChannel({"enabled": True}, "dl-01").payload(ev())
    assert set(brb) == {"system_id", "timestamp", "message"} and brb["system_id"] == "FILE-REAPER-DL-01"
    slack = SlackChannel({"enabled": True, "webhook_url": "https://hooks.slack.example/x"}, "dl-01").payload(ev("error"))
    assert slack["text"].startswith("[ERROR]") and slack["attachments"][0]["color"] == "#E74C3C"
    json.dumps(slack)
    mail = EmailChannel({"enabled": True, "to": ["ops@example.org"], "from": "r@example.org"}, "dl-01").build(ev())
    assert mail["To"] == "ops@example.org" and "[WARNING]" in mail["Subject"] and "area: raw" in mail.get_content()

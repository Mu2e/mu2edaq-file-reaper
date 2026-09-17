import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from mu2edaq_file_reaper.db import init_db
from mu2edaq_file_reaper.db.repo import HistoryRepo, TokenRepo, glob_to_like


def test_wal_and_concurrent_writers(tmp_path):
    db = init_db(f"sqlite:///{tmp_path / 'r.db'}")
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
    hist = HistoryRepo(db)
    errors = []

    def writer(n):
        try:
            for i in range(50):
                hist.record("action_delete", f"a{n}", path=f"/p/{n}/{i}", bytes_freed=i)
        except Exception as exc:
            errors.append(exc)
        finally:
            db.remove()
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows, total = hist.query(limit=5)
    assert total == 200 and len(rows) == 5
    db.dispose()


def test_history_query_filters_and_prune(repos):
    h = repos["history"]
    h.record("action_delete", "raw", path="/data/raw/run_001.dat", tier="critical", bytes_freed=5, scan_id="s1")
    h.record("action_compress", "raw", path="/data/raw/run_002.dat", tier="warning", scan_id="s1")
    h.record("scan_end", "logs", used_pct_after=42.0)
    h.record("action_delete", "logs", path="/daqlogs/x_%_y.log", scan_id="s2")
    rows, total = h.query(area="raw")
    assert total == 2 and rows[0]["event_type"] == "action_compress"          # newest first
    rows, total = h.query(path_glob="/data/raw/run_00?.dat")
    assert total == 2
    rows, total = h.query(path_glob="/daqlogs/x_%_y.log")
    assert total == 1                                                          # exact path with % literal
    rows, total = h.query(event_types=["action_delete"], tier="critical")
    assert total == 1 and rows[0]["path"].endswith("run_001.dat")
    rows, total = h.query(scan_id="s2")
    assert total == 1
    rows, total = h.query(limit=1, offset=1, newest_first=False)
    assert total == 4 and rows[0]["event_type"] == "action_compress"
    assert h.recent_usage("logs") == [{"ts": h.recent_usage("logs")[0]["ts"], "used_pct": 42.0}]
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert h.prune(future) == 4
    assert h.query()[1] == 0
    assert h.prune_days(0) == 0


def test_glob_to_like_escapes():
    assert glob_to_like("a*b?c") == "a%b_c"
    assert glob_to_like("50%_x\\") == "50\\%\\_x\\\\"


def test_token_repo_lifecycle(repos):
    t = repos["tokens"]
    rec = t.create("cli", "rpr_abcdefgh", "hash", ["read", "operate"], created_by="ui:admin")
    assert rec["active"] and rec["scopes"] == ["operate", "read"]
    cands = t.candidates("rpr_abcdefgh")
    assert len(cands) == 1 and cands[0]["token_hash"] == "hash"
    t.touch(rec["id"], "127.0.0.1")
    assert t.get(rec["id"])["last_used_ip"] == "127.0.0.1"
    expired = t.create("old", "rpr_zzzzzzzz", "h2", ["read"], expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert not t.get(expired["id"])["active"] and t.candidates("rpr_zzzzzzzz") == []
    assert t.count_active() == 1
    assert t.revoke(rec["id"])["active"] is False
    assert t.candidates("rpr_abcdefgh") == [] and t.revoke(999) is None
    assert len(t.list()) == 2 and len(t.list(include_revoked=False)) == 1


def test_exclusion_repo_expiry_and_reactivation(repos):
    e = repos["exclusions"]
    a = e.add("/d/a.dat", area="raw", reason="keep")
    assert a["kind"] == "path"
    g = e.add("*.raw", reason="global", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert g["kind"] == "glob" and g["area"] is None
    rules = e.active_rules()
    assert [r["pattern"] for r in rules] == ["/d/a.dat"]          # expired one deactivated
    assert len(e.list(include_inactive=True)) == 2
    assert e.remove(a["id"])["active"] is False and e.remove(12345) is None
    again = e.add("/d/a.dat", area="raw")
    assert again["id"] == a["id"] and again["active"]
    assert [x["pattern"] for x in e.list(area="raw")] == ["/d/a.dat"]


def test_area_state_and_notification_log(repos):
    a = repos["area_state"]
    st = a.load("raw", "/data/raw")
    assert st["paused"] is False and st["active_tiers"] == []
    a.set_disabled("raw", True, "x", "why")
    a.mark_scan("raw", "scan-1")
    assert a.all()["raw"]["disabled"] and a.all()["raw"]["last_scan_id"] == "scan-1"
    n = repos["notif_log"]
    n.record("slack", "warning", "t1", "sent", area="raw", event_key="k")
    n.record("slack", "warning", "t1", "suppressed_dup", event_key="k")
    n.record("email", "critical", "t2", "failed", error="boom")
    assert len(n.recent()) == 3 and n.recent(channel="email")[0]["error"] == "boom"
    assert set(n.last_sent()) == {"slack"}
    assert n.prune_days(0) == 0

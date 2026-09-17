"""Tests for the ``mu2edaq-reaper`` command line client.

* token cache: permissions, atomic save, round trip
* config precedence: CLI > env > YAML file > defaults
* end-to-end: the CLI against a real Flask/Werkzeug server built from the
  reaper engine with an in-memory SQLite database.
"""

import json
import os
import stat
import sys
import threading

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mu2edaq_reaper_cli import cli, tokencache  # noqa: E402
from mu2edaq_reaper_cli import config as configmod  # noqa: E402
from mu2edaq_reaper_cli.client import ReaperClient, ReaperConnectionError, ReaperError  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never touch the real home directory or a real config/token file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MU2EDAQ_REAPER_TOKEN_FILE", str(tmp_path / "tokens.yaml"))
    monkeypatch.setenv("MU2EDAQ_REAPER_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    for var in ("MU2EDAQ_REAPER_URL", "MU2EDAQ_REAPER_TOKEN", "MU2EDAQ_REAPER_ADMIN_TOKEN",
                "MU2EDAQ_REAPER_INSTANCE", "MU2EDAQ_REAPER_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    yield home


@pytest.fixture
def server(tmp_path):
    """A live reaper API server on 127.0.0.1:<random port>.

    Function-scoped on purpose: ``tests/conftest.py`` has an autouse fixture
    that resets the process-wide ``Settings`` and ``StateStore`` before every
    test, so the server must (re)apply its settings after that runs.
    """
    tmp_path = tmp_path / "reaper"
    (tmp_path / "area").mkdir(parents=True)
    for i in range(3):
        (tmp_path / "area" / f"file{i}.dat").write_bytes(b"x" * 1024 * (i + 1))

    from mu2edaq_file_reaper.settings import reset_settings
    from mu2edaq_file_reaper.config import build_areas
    from mu2edaq_file_reaper.db import init_db
    from mu2edaq_file_reaper.db.repo import (HistoryRepo, ExclusionRepo, AreaStateRepo,
                                             NotificationLogRepo, TokenRepo)
    from mu2edaq_file_reaper.exclusions import ExclusionRegistry
    from mu2edaq_file_reaper.notify import build_notifier
    from mu2edaq_file_reaper.reaper import Deps
    from mu2edaq_file_reaper.scheduler import Scheduler
    from mu2edaq_file_reaper.state import STORE
    from mu2edaq_file_reaper.usage import measure_usage
    from mu2edaq_file_reaper.web import create_app

    areas, issues = build_areas({"areas": [{"path": str(tmp_path / "area"), "settle_seconds": 0}]},
                                allow_shallow_root=True)
    S = reset_settings(admin_token="secret", areas=areas, allow_shallow_root=True, label="test")
    db = init_db("sqlite://")
    hist = HistoryRepo(db)
    ex = ExclusionRegistry(ExclusionRepo(db))
    ex.reload()
    n = build_notifier({}, "test", log_repo=NotificationLogRepo(db), history=hist)
    deps = Deps(store=STORE, history=hist, area_state=AreaStateRepo(db), exclusions=ex, notifier=n,
                measure_usage=lambda p: measure_usage(p))
    sched = Scheduler(deps, S)
    sched.run_now(areas[0].name)
    app = create_app(db=db, scheduler=sched, deps=deps, notifier=n, tokens=TokenRepo(db),
                     exclusions_repo=ExclusionRepo(db))

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, name="test-reaper-web", daemon=True)
    t.start()
    info = {"url": f"http://127.0.0.1:{srv.server_port}", "area": areas[0].name, "port": srv.server_port}
    yield info
    srv.shutdown()
    db.dispose()


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def run_json(capsys, *argv):
    rc, out, errtxt = run(capsys, "--json", *argv)
    assert rc == 0, errtxt
    return json.loads(out)


# --------------------------------------------------------------------------
# token cache
# --------------------------------------------------------------------------
class TestTokenCache:
    def test_path_from_env(self, tmp_path):
        assert tokencache.cache_path() == str(tmp_path / "tokens.yaml")

    def test_default_path_under_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MU2EDAQ_REAPER_TOKEN_FILE")
        p = tokencache.cache_path()
        assert p.startswith(str(tmp_path / "home"))
        assert p.endswith(os.path.join(".config", "mu2edaq", "file-reaper", "tokens.yaml"))

    def test_missing_file_is_empty(self):
        assert tokencache.load() == {}
        assert tokencache.get("http://x:1") is None
        assert tokencache.remove("http://x:1") is False

    def test_save_load_roundtrip_and_permissions(self, tmp_path):
        path = tokencache.save("http://host:5004/", "rpr_abc", "shifter")
        assert path == str(tmp_path / "tokens.yaml")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        entry = tokencache.get("http://host:5004")          # trailing slash normalised
        assert entry["token"] == "rpr_abc"
        assert entry["name"] == "shifter"
        assert entry["saved"].endswith("Z")
        raw = yaml.safe_load(open(path))
        assert list(raw) == ["http://host:5004"]
        listed = tokencache.list_all()
        assert "rpr_abc" not in json.dumps(listed)         # never leaks the token
        assert listed["http://host:5004"]["name"] == "shifter"

    def test_save_creates_private_parent(self, tmp_path, monkeypatch):
        deep = tmp_path / "deep" / "er" / "tokens.yaml"
        monkeypatch.setenv("MU2EDAQ_REAPER_TOKEN_FILE", str(deep))
        tokencache.save("http://a:1", "rpr_1", "n")
        assert stat.S_IMODE(os.stat(deep.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(deep).st_mode) == 0o600
        assert not [p for p in deep.parent.iterdir() if p.name.startswith(".tokens.")]

    def test_multiple_urls_and_remove(self):
        tokencache.save("http://a:1", "rpr_a", "a")
        tokencache.save("http://b:2", "rpr_b", "b")
        assert set(tokencache.load()) == {"http://a:1", "http://b:2"}
        assert tokencache.remove("http://a:1/") is True
        assert set(tokencache.load()) == {"http://b:2"}

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
    def test_refuses_group_or_world_readable(self, tmp_path):
        path = tokencache.save("http://a:1", "rpr_a", "a")
        os.chmod(path, 0o644)
        with pytest.raises(tokencache.TokenCacheError, match="readable by others"):
            tokencache.load()
        os.chmod(path, 0o640)
        with pytest.raises(tokencache.TokenCacheError):
            tokencache.get("http://a:1")
        os.chmod(path, 0o600)
        assert tokencache.get("http://a:1")["token"] == "rpr_a"

    def test_rejects_empty_token(self):
        with pytest.raises(tokencache.TokenCacheError):
            tokencache.save("http://a:1", "   ", "a")


# --------------------------------------------------------------------------
# config precedence
# --------------------------------------------------------------------------
class TestConfig:
    def test_defaults(self):
        s = configmod.resolve()
        assert s.url == "http://localhost:5004"
        assert s.timeout == 10.0
        assert s.token is None
        assert s.config_file is None
        assert s.sources["url"] == "default"

    def test_file_then_env_then_cli(self, tmp_path, monkeypatch):
        cfg = tmp_path / "mu2edaq-reaper.yaml"
        cfg.write_text(yaml.safe_dump({"url": "http://from-file:1", "timeout": 3,
                                       "instance": "filelabel"}))
        monkeypatch.setenv("MU2EDAQ_REAPER_CONFIG", str(cfg))
        s = configmod.resolve()
        assert s.url == "http://from-file:1"
        assert s.timeout == 3.0
        assert s.instance == "filelabel"
        assert s.sources["url"] == "config"

        monkeypatch.setenv("MU2EDAQ_REAPER_URL", "http://from-env:2/")
        monkeypatch.setenv("MU2EDAQ_REAPER_TIMEOUT", "7.5")
        s = configmod.resolve()
        assert s.url == "http://from-env:2"                # trailing slash stripped
        assert s.timeout == 7.5
        assert s.sources["url"] == "env"

        s = configmod.resolve(url="from-cli:3", timeout=1)
        assert s.url == "http://from-cli:3"                # scheme added
        assert s.timeout == 1.0
        assert s.sources["url"] == "cli"

    def test_search_path_cwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MU2EDAQ_REAPER_CONFIG")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "mu2edaq-reaper.yaml").write_text("url: http://cfgdir:9\n")
        assert configmod.resolve().url == "http://cfgdir:9"
        (tmp_path / "mu2edaq-reaper.yaml").write_text("url: http://cwd:8\n")
        assert configmod.resolve().url == "http://cwd:8"

    def test_explicit_missing_config_is_an_error(self, tmp_path):
        with pytest.raises(configmod.ConfigError):
            configmod.resolve(config=str(tmp_path / "nope.yaml"))

    def test_token_precedence(self, monkeypatch):
        tokencache.save("http://h:1", "rpr_cache", "c")
        s = configmod.resolve(url="http://h:1")
        assert (s.token, s.sources["token"]) == ("rpr_cache", "cache")
        s = configmod.resolve(url="http://other:1")
        assert s.token is None and "token" not in s.sources
        monkeypatch.setenv("MU2EDAQ_REAPER_TOKEN", "rpr_env")
        s = configmod.resolve(url="http://h:1")
        assert (s.token, s.sources["token"]) == ("rpr_env", "env")
        s = configmod.resolve(url="http://h:1", token="rpr_cli")
        assert (s.token, s.sources["token"]) == ("rpr_cli", "cli")

    def test_admin_token_from_env(self, monkeypatch):
        monkeypatch.setenv("MU2EDAQ_REAPER_ADMIN_TOKEN", "sekrit")
        s = configmod.resolve()
        assert s.admin_token == "sekrit" and s.sources["admin_token"] == "env"
        assert s.as_dict()["admin_token"] == "***"

    def test_instance_via_discovery(self, monkeypatch):
        recs = [{"name": "File Reaper (daq01)", "host": "daq01.fnal.gov", "port": 5004,
                 "scheme": "http", "version": "1.0", "meta": {"instance": "daq01", "api_port": "5005"}},
                {"name": "File Reaper (daq02)", "host": "daq02", "port": 5004, "scheme": "http",
                 "meta": {"instance": "daq02", "api_port": "5004"}}]
        monkeypatch.setattr(configmod, "discover_instances", lambda timeout=2.0: recs)
        s = configmod.resolve(instance="daq01")
        assert s.url == "http://daq01.fnal.gov:5005"
        assert s.sources["url"] == "discovery"
        s = configmod.resolve(instance="DAQ02")                      # loose match on name
        assert s.url == "http://daq02:5004"
        with pytest.raises(configmod.ConfigError, match="no file-reaper instance"):
            configmod.resolve(instance="nothere")
        # explicit url beats instance
        assert configmod.resolve(url="http://x:1", instance="daq01").url == "http://x:1"

    def test_instance_without_discovery_package(self, monkeypatch):
        def boom(timeout=2.0):
            raise ImportError("no module")
        monkeypatch.setattr(configmod, "discover_instances", boom)
        with pytest.raises(configmod.ConfigError, match="mu2edaq_discovery"):
            configmod.resolve(instance="x")


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def test_formatting_helpers():
    assert cli.fmt_bytes(1288490188.8) == "1.2 GiB"
    assert cli.fmt_bytes(512) == "512 B"
    assert cli.fmt_bytes(None) == "-"
    assert cli.fmt_duration(3 * 86400 + 4 * 3600 + 5) == "3d 4h"
    assert cli.fmt_duration(4 * 3600 + 12 * 60) == "4h 12m"
    assert cli.fmt_duration(65) == "1m 5s"
    assert cli.fmt_duration(7) == "7s"
    assert cli.fmt_duration(None) == "-"
    assert cli.fmt_ts("2026-09-11T14:02:11Z") == "2026-09-11 14:02:11"
    assert cli.fmt_ts(0) == "1970-01-01 00:00:00"
    t = cli.table([[1, "ab"], [22, "c"]], ["N", "S"], right=(0,))
    assert t.splitlines()[0] == " N  S"          # right-aligned columns align the header too
    assert t.splitlines()[2] == " 1  ab"


# --------------------------------------------------------------------------
# end to end against a live server
# --------------------------------------------------------------------------
class TestEndToEnd:
    def test_no_command_prints_help(self, capsys):
        rc, out, _ = run(capsys)
        assert rc == 2 and "usage:" in out

    def test_unreachable_is_exit_2(self, capsys):
        rc, out, errtxt = run(capsys, "--url", "http://127.0.0.1:1", "--timeout", "2", "status")
        assert rc == 2
        assert "cannot reach http://127.0.0.1:1" in errtxt

    def test_client_errors(self, server):
        c = ReaperClient(server["url"])
        with pytest.raises(ReaperError) as ei:
            c.area("nope")
        assert ei.value.status == 404 and "unknown area" in ei.value.message
        with pytest.raises(ReaperError) as ei:
            c.tokens()
        assert ei.value.status == 401
        with pytest.raises(ReaperConnectionError):
            ReaperClient("http://127.0.0.1:1", timeout=2).health()

    def test_status_and_areas_open_read(self, server, capsys):
        rc, out, _ = run(capsys, "--url", server["url"], "status")
        # the scheduler thread is not started in the test server -> "degraded" -> exit 1
        assert rc == 1, out
        assert "mu2edaq-file-reaper" in out and "status=degraded" in out and "scheduler alive=no" in out
        assert server["area"] in out
        assert "AREA" in out and "STATE" in out

        rc, out, _ = run(capsys, "--url", server["url"], "areas")
        assert rc == 0
        assert server["area"] in out

        data = run_json(capsys, "--url", server["url"], "areas")
        assert data["total"] == 1 and data["areas"][0]["name"] == server["area"]

        rc, out, _ = run(capsys, "--url", server["url"], "area", server["area"])
        assert rc == 0
        assert "usage:" in out and "last scan:" in out and "queues:" in out

        rc, out, errtxt = run(capsys, "--url", server["url"], "area", "does-not-exist")
        assert rc == 1 and "unknown area" in errtxt

    def test_version_and_config(self, server, capsys):
        rc, out, _ = run(capsys, "--url", server["url"], "version")
        assert rc == 0 and "mu2edaq-reaper" in out and "server mu2edaq-file-reaper" in out
        rc, out, _ = run(capsys, "--url", server["url"], "config")
        assert rc == 0 and "areas" in out and server["area"] in out
        rc, out, _ = run(capsys, "--url", server["url"], "config", "--local")
        assert rc == 0 and server["url"] in out and "[cli]" in out

    def test_mutations_need_auth(self, server, capsys):
        rc, out, errtxt = run(capsys, "--url", server["url"], "pause", server["area"])
        assert rc == 1
        assert "token login" in errtxt and "401" in errtxt

    def test_token_workflow(self, server, capsys, tmp_path):
        url = server["url"]
        area = server["area"]

        # ---- mint the first token with the admin secret ----------------------
        data = run_json(capsys, "--url", url, "--admin-token", "secret", "token", "create",
                        "cli-test", "--scopes", "read,operate,admin")
        token = data["token"]
        assert token.startswith("rpr_")
        assert data["record"]["scopes"] == ["admin", "operate", "read"]

        # wrong admin secret -> 401 with hint
        rc, out, errtxt = run(capsys, "--url", url, "--admin-token", "wrong", "token", "create", "x")
        assert rc == 1 and "token login" in errtxt

        # ---- store it, then use it implicitly via the cache -----------------
        rc, out, _ = run(capsys, "--url", url, "token", "login", token)
        assert rc == 0
        assert token[:8] in out and token not in out               # only the prefix is shown
        assert "identity: token:cli-test" in out
        assert stat.S_IMODE(os.stat(tmp_path / "tokens.yaml").st_mode) == 0o600

        rc, out, _ = run(capsys, "--url", url, "token", "whoami")
        assert rc == 0
        assert "token:cli-test" in out and "(token from cache)" in out
        who = run_json(capsys, "--url", url, "token", "whoami")
        assert who["kind"] == "token" and who["name"] == "cli-test"

        # ---- read endpoints with the token ----------------------------------
        rc, out, _ = run(capsys, "--url", url, "--token", token, "areas")
        assert rc == 0 and area in out
        rc, out, _ = run(capsys, "--url", url, "history")
        assert rc == 0 and "history:" in out and "token_create" in out
        hist = run_json(capsys, "--url", url, "history", "--type", "token_create", "--limit", "5")
        assert hist["total"] >= 1 and all(e["event_type"] == "token_create" for e in hist["events"])
        rc, out, _ = run(capsys, "--url", url, "history", "--csv", "-")
        assert rc == 0 and out.splitlines()[0].startswith("id,ts,area,event_type")
        csv_file = tmp_path / "h.csv"
        rc, out, _ = run(capsys, "--url", url, "history", "--csv", str(csv_file))
        assert rc == 0 and csv_file.exists() and "wrote" in out
        rc, out, _ = run(capsys, "--url", url, "history", "--types")
        assert rc == 0 and "token_create" in out.split()

        # ---- exclusions ------------------------------------------------------
        rc, out, _ = run(capsys, "--url", url, "exclude", "add", "/keep/*.root",
                         "--reason", "important", "--expires-in", "1d")
        assert rc == 0 and "added exclusion #" in out and "glob" in out
        ex = run_json(capsys, "--url", url, "exclude", "list")
        assert len(ex["exclusions"]) == 1
        ex_id = ex["exclusions"][0]["id"]
        assert ex["exclusions"][0]["pattern"] == "/keep/*.root"
        rc, out, _ = run(capsys, "--url", url, "exclude", "list")
        assert rc == 0 and "/keep/*.root" in out and "important" in out
        rc, out, _ = run(capsys, "--url", url, "exclude", "add", "/x", "--area", "nope")
        assert rc == 1
        rc, out, _ = run(capsys, "--url", url, "exclude", "rm", str(ex_id))
        assert rc == 0 and "removed exclusion" in out
        rc, out, _ = run(capsys, "--url", url, "exclude", "list")
        assert rc == 0 and "(no exclusions)" in out
        rc, out, _ = run(capsys, "--url", url, "exclude", "list", "--all")
        assert rc == 0 and "/keep/*.root" in out
        rc, out, errtxt = run(capsys, "--url", url, "exclude", "rm", "99999")
        assert rc == 1 and "not found" in errtxt

        # ---- pause / resume --------------------------------------------------
        rc, out, _ = run(capsys, "--url", url, "pause", area, "--reason", "maintenance")
        assert rc == 0 and f"pause {area}: ok" in out and "maintenance" in out
        st = run_json(capsys, "--url", url, "area", area)
        assert st["area"]["paused"] is True and st["area"]["paused_reason"] == "maintenance"
        rc, out, _ = run(capsys, "--url", url, "areas")
        assert "paused" in out
        rc, out, _ = run(capsys, "--url", url, "resume", area)
        assert rc == 0 and f"resume {area}: ok" in out
        assert run_json(capsys, "--url", url, "area", area)["area"]["paused"] is False

        # ---- queues / plan / rescan / disable / enable ------------------------
        rc, out, _ = run(capsys, "--url", url, "queues")
        assert rc == 0 and "queued: compress=" in out
        rc, out, _ = run(capsys, "--url", url, "plan", area, "--limit", "5")
        assert rc == 0 and f"dry-run plan for {area}" in out
        rc, out, _ = run(capsys, "--url", url, "rescan", area)
        assert rc == 0 and f"rescan {area}: ok" in out
        rc, out, _ = run(capsys, "--url", url, "disable", area, "--reason", "broken disk")
        assert rc == 0 and f"disable {area}: ok" in out
        assert run_json(capsys, "--url", url, "area", area)["area"]["disabled"] is True
        rc, out, _ = run(capsys, "--url", url, "enable", area)
        assert rc == 0
        assert run_json(capsys, "--url", url, "area", area)["area"]["disabled"] is False

        # ---- notifications ---------------------------------------------------
        rc, out, _ = run(capsys, "--url", url, "notify", "status")
        assert rc == 0 and "notifications:" in out
        rc, out, errtxt = run(capsys, "--url", url, "notify", "test", "no-such-channel")
        assert rc == 1 and "unknown channel" in errtxt

        # ---- token list / create --save / revoke ------------------------------
        rc, out, _ = run(capsys, "--url", url, "token", "list")
        assert rc == 0 and "cli-test" in out and token not in out
        rc, out, _ = run(capsys, "--url", url, "token", "create", "second", "--scopes", "read", "--save")
        assert rc == 0 and "token: rpr_" in out and "saved to" in out
        cached = tokencache.get(url)
        assert cached["name"] == "second" and cached["token"] != token
        # the cached token is now the read-only one: operate call fails with 403 hint
        rc, out, errtxt = run(capsys, "--url", url, "pause", area)
        assert rc == 1 and "403" in errtxt and "token login" in errtxt
        # explicit --token overrides the cache
        toks = run_json(capsys, "--url", url, "--token", token, "token", "list")
        second_id = [t for t in toks["tokens"] if t["name"] == "second"][0]["id"]
        rc, out, _ = run(capsys, "--url", url, "--token", token, "token", "revoke", str(second_id))
        assert rc == 0 and f"revoked token #{second_id}" in out
        rc, out, _ = run(capsys, "--url", url, "token", "whoami")
        assert rc == 1                                            # revoked -> anonymous
        rc, out, errtxt = run(capsys, "--url", url, "--token", token, "token", "revoke", "424242")
        assert rc == 1 and "not found" in errtxt

        # ---- logout ------------------------------------------------------------
        rc, out, _ = run(capsys, "--url", url, "token", "logout")
        assert rc == 0 and "removed cached token" in out
        assert tokencache.get(url) is None
        rc, out, _ = run(capsys, "--url", url, "token", "logout")
        assert rc == 0 and "no cached token" in out

    def test_login_from_env_token_and_refused_unsafe_cache(self, server, capsys, tmp_path, monkeypatch):
        data = run_json(capsys, "--url", server["url"], "--admin-token", "secret", "token", "create",
                        "envtok", "--scopes", "read")
        monkeypatch.setenv("MU2EDAQ_REAPER_TOKEN", data["token"])
        rc, out, _ = run(capsys, "--url", server["url"], "token", "whoami")
        assert rc == 0 and "token:envtok" in out and "(token from env)" in out
        monkeypatch.delenv("MU2EDAQ_REAPER_TOKEN")
        tokencache.save(server["url"], data["token"], "envtok")
        os.chmod(tmp_path / "tokens.yaml", 0o644)
        rc, out, errtxt = run(capsys, "--url", server["url"], "token", "whoami")
        assert rc == 1 and "readable by others" in errtxt

    def test_instances_without_discovery(self, capsys, monkeypatch):
        def boom(timeout=2.0):
            raise ImportError("no module named mu2edaq_discovery")
        monkeypatch.setattr(configmod, "discover_instances", boom)
        rc, out, errtxt = run(capsys, "instances")
        assert rc == 1 and "mu2edaq_discovery" in errtxt

    def test_instances_table(self, capsys, monkeypatch):
        recs = [{"name": "File Reaper (daq01)", "host": "daq01", "port": 5004, "scheme": "http",
                 "version": "1.2.3", "meta": {"instance": "daq01", "api_port": "5005", "dry_run": "false",
                                             "areas": "4"}}]
        monkeypatch.setattr(configmod, "discover_instances", lambda timeout=2.0: recs)
        rc, out, _ = run(capsys, "instances")
        assert rc == 0 and "daq01" in out and "5005" in out and "1.2.3" in out
        assert "http://daq01:5005" in out

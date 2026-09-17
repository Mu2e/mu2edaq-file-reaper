"""Every page renders, every API route answers, and the auth model holds."""

import os

import pytest

from mu2edaq_file_reaper.db.repo import ExclusionRepo, TokenRepo
from mu2edaq_file_reaper.scheduler import Scheduler
from mu2edaq_file_reaper.settings import get_settings
from mu2edaq_file_reaper.state import AREA_STATE_KEYS, STORE
from mu2edaq_file_reaper.web import create_app
from mu2edaq_file_reaper.web.nav import NAV_ITEMS

PAGES = ["/", "/queues", "/history", "/exclusions", "/notifications", "/config", "/api",
         "/about", "/sitemap", "/login"]
READ_API = ["/api/v1/health", "/api/v1/version", "/api/v1/whoami", "/api/v1/state", "/api/v1/areas",
            "/api/v1/queues", "/api/v1/exclusions", "/api/v1/history", "/api/v1/history/types",
            "/api/v1/notifications", "/api/v1/config"]


@pytest.fixture
def web(tree, area_factory, deps_factory, repos, fake_disk, notifier):
    tree.file("sub/a.dat", size=4096, age_days=3)
    tree.file("b.dat", size=4096, age_days=9)
    area, _ = area_factory(tree.root, tiers={"critical": {"policy": "LRU-Delete", "min_age": "1d"}})
    S = get_settings()
    S.apply(areas=[area], admin_token="secret", label="test")
    fake_disk.set_used_pct(20)
    deps = deps_factory()
    sched = Scheduler(deps, S, history=repos["history"])
    sched.run_now(area.name)
    app = create_app(db=repos["db"], scheduler=sched, deps=deps, notifier=notifier,
                     tokens=TokenRepo(repos["db"]), exclusions_repo=ExclusionRepo(repos["db"]))
    app.config.update(TESTING=True)
    return {"app": app, "area": area, "sched": sched, "repos": repos, "disk": fake_disk}


def login(client, token="secret"):
    r = client.post("/login", data={"token": token})
    csrf = client.get("/").data.decode().split('name="csrf-token" content="')[1].split('"')[0]
    return r, csrf


def test_every_page_renders_and_navbar_links_every_page(web):
    c = web["app"].test_client()
    for url in PAGES + [f"/areas/{web['area'].name}"]:
        r = c.get(url)
        assert r.status_code == 200, url
    home = c.get("/").data.decode()
    # Count the opening <nav> element, not 'class="navbar', which also matches
    # the navbar-brand/-toggler/-nav/-text Bootstrap classes inside it.
    assert home.count('<nav class="navbar') == 1
    with web["app"].test_request_context():
        from flask import url_for
        for ep, label, _icon in NAV_ITEMS:
            assert url_for(ep) in home, ep
    assert c.get("/areas/nope").status_code == 404
    assert c.get("/nope").status_code == 404
    assert c.get("/api/v1/nope").status_code == 404 and c.get("/api/v1/nope").is_json


def test_tokens_page_needs_admin_login(web):
    c = web["app"].test_client()
    r = c.get("/tokens")
    assert r.status_code in (302, 303) and "/login" in r.headers["Location"]
    assert c.post("/login", data={"token": "wrong"}).status_code in (200, 401, 403)
    r, _ = login(c)
    assert r.status_code in (302, 303)
    assert c.get("/tokens").status_code == 200
    assert "ADMIN" in c.get("/").data.decode()
    c.post("/logout")
    assert c.get("/tokens").status_code in (302, 303)


def test_login_redirect_only_allows_relative_next(web):
    c = web["app"].test_client()
    r = c.post("/login?next=https://evil.example/x", data={"token": "secret"})
    assert "evil.example" not in (r.headers.get("Location") or "")


def test_read_api_open_and_snapshot_shape(web):
    c = web["app"].test_client()
    for url in READ_API:
        r = c.get(url)
        assert r.status_code == 200 and r.is_json, url
    state = c.get("/api/v1/state").get_json()
    assert state["label"] == "test" and state["summary"]["total"] == 1
    assert set(state["areas"][0]) == AREA_STATE_KEYS
    area = c.get(f"/api/v1/areas/{web['area'].name}").get_json()
    assert area["config"]["tiers"]["critical"]["policy"] == "LRU-Delete"
    assert isinstance(area["usage_history"], list)
    assert c.get("/api/v1/health").get_json()["status"] in ("ok", "degraded")
    assert c.get("/api/v1/whoami").get_json()["kind"] == "anonymous"
    cfg = c.get("/api/v1/config").get_json()
    assert cfg["admin_token"] == "***" and cfg["areas"][0]["name"] == web["area"].name


def test_open_read_can_be_closed(web):
    get_settings().api_open_read = False
    c = web["app"].test_client()
    assert c.get("/api/v1/state").status_code == 401
    assert c.get("/api/v1/health").status_code == 200          # liveness stays open
    assert c.get("/api/v1/state", headers={"X-Admin-Token": "secret"}).status_code == 200


def test_mutations_need_auth_and_csrf(web):
    c = web["app"].test_client()
    name = web["area"].name
    assert c.post(f"/api/v1/areas/{name}/pause", json={}).status_code == 401
    assert c.get("/api/v1/tokens").status_code == 401
    _, csrf = login(c)
    assert c.post(f"/api/v1/areas/{name}/pause", json={"reason": "x"}).status_code == 403   # no CSRF
    r = c.post(f"/api/v1/areas/{name}/pause", json={"reason": "x"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.get_json()["actor"] == "ui:admin"
    assert web["sched"].runtime(name).paused
    r = c.post(f"/api/v1/areas/{name}/resume", json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and not web["sched"].runtime(name).paused
    assert c.post("/api/v1/areas/nope/pause", json={}, headers={"X-CSRF-Token": csrf}).status_code == 404


def test_token_lifecycle_and_scopes(web):
    app = web["app"]
    name = web["area"].name
    admin = app.test_client()
    hdr = {"X-Admin-Token": "secret"}
    r = admin.post("/api/v1/tokens", json={"name": "ro", "scopes": ["read"]}, headers=hdr)
    assert r.status_code == 201
    ro = r.get_json()["token"]
    r = admin.post("/api/v1/tokens", json={"name": "ops", "scopes": "read,operate", "expires_in": "1d"}, headers=hdr)
    ops = r.get_json()["token"]
    assert r.get_json()["record"]["expires_at"]
    r = admin.post("/api/v1/tokens", json={"name": "root", "scopes": ["admin"]}, headers=hdr)
    root = r.get_json()["token"]
    assert admin.post("/api/v1/tokens", json={"name": "bad", "scopes": ["god"]}, headers=hdr).status_code == 400
    assert admin.post("/api/v1/tokens", json={"scopes": ["read"]}, headers=hdr).status_code == 400

    def bearer(t):
        return {"Authorization": f"Bearer {t}"}
    c = app.test_client()
    assert c.get("/api/v1/whoami", headers=bearer(ro)).get_json() == {"kind": "token", "name": "ro",
                                                                     "scopes": ["read"], "actor": "token:ro"}
    assert c.post(f"/api/v1/areas/{name}/pause", json={}, headers=bearer(ro)).status_code == 403
    assert c.post(f"/api/v1/areas/{name}/pause", json={}, headers=bearer(ops)).status_code == 200
    assert c.post(f"/api/v1/areas/{name}/disable", json={}, headers=bearer(ops)).status_code == 403
    assert c.post(f"/api/v1/areas/{name}/disable", json={"reason": "r"}, headers=bearer(root)).status_code == 200
    assert c.post(f"/api/v1/areas/{name}/enable", json={}, headers=bearer(root)).status_code == 200
    assert c.post(f"/api/v1/areas/{name}/resume", json={}, headers=bearer(ops)).status_code == 200
    assert c.post(f"/api/v1/areas/{name}/rescan", json={}, headers=bearer(ops)).status_code == 200
    assert c.get("/api/v1/whoami", headers=bearer("rpr_garbage")).get_json()["kind"] == "anonymous"
    listing = c.get("/api/v1/tokens", headers=bearer(root)).get_json()["tokens"]
    assert {t["name"] for t in listing} == {"ro", "ops", "root"}
    assert all("token_hash" not in t and "token" not in t for t in listing)
    ro_id = next(t["id"] for t in listing if t["name"] == "ro")
    assert c.delete(f"/api/v1/tokens/{ro_id}", headers=bearer(root)).status_code == 200
    assert c.get("/api/v1/whoami", headers=bearer(ro)).get_json()["kind"] == "anonymous"   # cache invalidated
    assert c.delete("/api/v1/tokens/9999", headers=bearer(root)).status_code == 404
    rows, _ = web["repos"]["history"].query(event_types=["token_create", "token_revoke", "area_pause", "area_disable"])
    actors = {r["actor"] for r in rows}
    assert "ui:admin" in actors and "token:ops" in actors and "token:root" in actors


def test_exclusions_dry_run_history_and_notifications(web):
    app = web["app"]
    name = web["area"].name
    c = app.test_client()
    hdr = {"X-Admin-Token": "secret"}
    r = c.post("/api/v1/exclusions", json={"pattern": "*.keep", "reason": "demo", "expires_in": "2h"}, headers=hdr)
    assert r.status_code == 201 and r.get_json()["exclusion"]["kind"] == "glob"
    ex_id = r.get_json()["exclusion"]["id"]
    assert c.post("/api/v1/exclusions", json={}, headers=hdr).status_code == 400
    assert c.post("/api/v1/exclusions", json={"pattern": "/x", "area": "nope"}, headers=hdr).status_code == 404
    assert c.post("/api/v1/exclusions", json={"pattern": "/x", "expires_in": "soon"}, headers=hdr).status_code == 400
    assert len(c.get("/api/v1/exclusions").get_json()["exclusions"]) == 1
    assert c.delete(f"/api/v1/exclusions/{ex_id}", headers=hdr).status_code == 200
    assert c.get("/api/v1/exclusions").get_json()["exclusions"] == []
    assert len(c.get("/api/v1/exclusions?all=1").get_json()["exclusions"]) == 1
    assert c.delete("/api/v1/exclusions/9999", headers=hdr).status_code == 404

    web["disk"].set_used_pct(95)
    r = c.post(f"/api/v1/dry-run/{name}", headers=hdr)
    assert r.status_code == 200
    body = r.get_json()
    assert body["dry_run"] and body["report"]["acted"] >= 1 and body["state"]["dry_run"] is True
    assert os.path.exists(os.path.join(web["area"].real_path, "b.dat"))
    assert c.post("/api/v1/dry-run/nope", headers=hdr).status_code == 404

    h = c.get("/api/v1/history?type=action_delete&limit=5").get_json()
    assert h["total"] >= 1 and all(e["outcome"] == "dry_run" for e in h["events"])
    assert c.get("/api/v1/history?since=24h&area=" + name).get_json()["total"] >= 1
    assert c.get("/api/v1/history?limit=abc").status_code == 400
    csv = c.get("/api/v1/history?format=csv&limit=3")
    assert csv.mimetype == "text/csv" and csv.data.decode().splitlines()[0].startswith("id,ts,area")
    assert "scan_end" in c.get("/api/v1/history/types").get_json()["types"]

    q = c.get(f"/api/v1/queues?area={name}&kind=delete").get_json()
    assert q["total"] >= 1 and q["entries"][0]["area"] == name
    entry = q["entries"][0]
    assert c.get(f"/api/v1/queues/{entry['id']}").get_json()["path"] == entry["path"]
    assert c.get("/api/v1/queues/nothing").status_code == 404

    n = c.get("/api/v1/notifications").get_json()
    assert {ch["name"] for ch in n["channels"]} >= {"slack", "email"} or n["channels"] == []
    r = c.post("/api/v1/notifications/test", json={"channel": "nope"}, headers=hdr)
    assert r.status_code in (404, 200)


def test_api_only_port_wrapper(web):
    from mu2edaq_file_reaper.web import _api_only_wrapper
    wsgi = _api_only_wrapper(web["app"])
    status = {}

    def start(st, headers):
        status["st"] = st
    body = b"".join(wsgi({"PATH_INFO": "/", "REQUEST_METHOD": "GET", "SERVER_NAME": "x",
                          "SERVER_PORT": "80", "wsgi.url_scheme": "http", "wsgi.input": None}, start))
    assert status["st"].startswith("404") and b"api/v1 only" in body

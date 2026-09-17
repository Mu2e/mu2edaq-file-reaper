"""Flask application factory and a threaded server helper.

``create_app()`` receives every engine object it needs explicitly, so tests can
build an app around fakes.  ``serve()`` starts one Werkzeug server per
configured port in daemon threads and returns handles with ``.shutdown()``.
"""

import logging
import os
import secrets
import threading
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, request

from .. import __version__
from ..auth import TokenCache
from ..settings import get_settings
from .nav import NAV_ITEMS

log = logging.getLogger("reaper.web")


def create_app(db=None, scheduler=None, deps=None, notifier=None, tokens=None,
               exclusions_repo=None, history=None, secret_key: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    settings = get_settings()
    app.config.update(
        SECRET_KEY=secret_key or os.environ.get("MU2EDAQ_FILE_REAPER_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    app.extensions["reaper"] = {
        "db": db, "scheduler": scheduler, "deps": deps, "notifier": notifier,
        "tokens": tokens, "exclusions_repo": exclusions_repo,
        "history": history if history is not None else (deps.history if deps else None),
        "token_cache": TokenCache(),
    }

    from . import api_v1, views
    app.register_blueprint(views.bp)
    app.register_blueprint(api_v1.bp)

    @app.context_processor
    def inject_globals():
        from .auth import csrf_token, is_admin_session
        s = get_settings()
        return {"nav_items": NAV_ITEMS, "version": __version__, "label": s.label,
                "is_admin": is_admin_session(), "csrf_token": csrf_token(),
                "dry_run": s.dry_run, "api_port": s.effective_api_port}

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return {"error": "not found"}, 404
        return render_template("error.html", title="Not Found", code=404, message="Page not found"), 404

    @app.errorhandler(405)
    def not_allowed(_exc):
        if request.path.startswith("/api/"):
            return {"error": "method not allowed"}, 405
        return render_template("error.html", title="Not Allowed", code=405, message="Method not allowed"), 405

    @app.teardown_appcontext
    def _remove_session(_exc):
        if db is not None:
            try:
                db.remove()
            except Exception:
                pass

    return app


def reaper_ext() -> Dict[str, Any]:
    from flask import current_app
    return current_app.extensions["reaper"]


class ServerHandle:
    def __init__(self, server, thread: threading.Thread, port: int, api_only: bool) -> None:
        self.server = server
        self.thread = thread
        self.port = port
        self.api_only = api_only

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass


def _api_only_wrapper(app: Flask):
    """WSGI wrapper that serves only /api/* (for a dedicated API port)."""
    def wsgi(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if not path.startswith("/api/"):
            start_response("404 NOT FOUND", [("Content-Type", "application/json")])
            return [b'{"error": "this port serves /api/v1 only"}']
        return app.wsgi_app(environ, start_response)
    return wsgi


def serve(app: Flask, settings) -> List[ServerHandle]:
    from werkzeug.serving import make_server
    handles = []
    web = make_server(settings.web_host, settings.web_port, app, threaded=True)
    t = threading.Thread(target=web.serve_forever, name="reaper-web", daemon=True)
    t.start()
    handles.append(ServerHandle(web, t, settings.web_port, False))
    if settings.api_port and settings.api_port != settings.web_port:
        api = make_server(settings.web_host, settings.api_port, _api_only_wrapper(app), threaded=True)
        t2 = threading.Thread(target=api.serve_forever, name="reaper-api", daemon=True)
        t2.start()
        handles.append(ServerHandle(api, t2, settings.api_port, True))
    return handles

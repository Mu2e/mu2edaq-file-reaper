"""Mu2e DAQ service discovery — best effort.

Advertises the HTTP port (and the API port, instance label and area count in
``meta``) so every reaper on the cluster shows up in ``mu2edaq-discover`` scans
and the control-room browser.  ``mu2edaq-discovery`` is not on PyPI, so a
missing package must never block startup.
"""

import logging
from typing import Optional

from . import __version__

log = logging.getLogger("reaper.discovery")


def start_responder(settings) -> Optional[object]:
    if not settings.discovery_enabled:
        return None
    try:
        from mu2edaq_discovery import Responder
        name = settings.discovery_name or f"File Reaper ({settings.label})"
        meta = {
            "instance": str(settings.label),
            "api_port": str(settings.effective_api_port),
            "api_path": "/api/v1",
            "areas":    str(len(settings.areas)),
            "dry_run":  "true" if settings.dry_run else "false",
        }
        responder = Responder(name=name, app=settings.discovery_app,
                              port=settings.web_port, scheme="http",
                              version=__version__, meta=meta)
        responder.start()
        log.info("discovery responder started (app=%s, name=%s)", settings.discovery_app, name)
        return responder
    except Exception as exc:
        log.info("discovery responder not started: %s", exc)
        return None


def stop_responder(responder) -> None:
    if responder is not None:
        try:
            responder.stop()
        except Exception as exc:
            log.warning("discovery responder stop failed: %s", exc)

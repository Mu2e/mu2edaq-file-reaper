"""mu2edaq-reaper — command line client for mu2edaq-file-reaper instances.

Talks only to the REST API (``/api/v1``) over HTTP with a bearer token, so it
works from any host on the DAQ network.  Endpoints are found via
``mu2edaq-discovery`` when no URL is given; tokens are cached in
``~/.config/mu2edaq/file-reaper/tokens.yaml`` (owner-only permissions).
"""

from mu2edaq_file_reaper import __version__  # noqa: F401  single version source

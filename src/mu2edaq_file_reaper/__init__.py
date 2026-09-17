"""mu2edaq-file-reaper — policy-driven disk cleanup for the Mu2e DAQ.

Monitors configured disk areas, alarms the DAQ when used space crosses the
``warning`` / ``critical`` / ``full`` tiers, builds ordered compression and
deletion queues from the files in each area, and acts on those queues until the
tier's low-water mark is reached.  Every decision and action is written to an
audit history that the web UI, the JSON API and the ``mu2edaq-reaper`` command
line tool can query.
"""

from datetime import datetime, timezone

__version__ = "0.1.0"

#: Human name used in discovery, log lines and page titles.
APP_NAME = "mu2edaq-file-reaper"

#: Process start time, for the uptime shown on the About page and /api/v1/health.
START_TIME = datetime.now(timezone.utc)

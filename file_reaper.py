#!/usr/bin/env python3
"""Entry-point shim for mu2edaq-file-reaper.

The control room runs ``python file_reaper.py`` from this directory; the
package itself lives under ``src/``.  Equivalent invocations:

    python file_reaper.py --config config/mu2edaq-file-reaper.yaml
    python -m mu2edaq_file_reaper --config ...
    mu2edaq-file-reaper --config ...          (console script, after pip install -e .)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from mu2edaq_file_reaper.cli import main  # noqa: E402

if __name__ == "__main__":
    main()

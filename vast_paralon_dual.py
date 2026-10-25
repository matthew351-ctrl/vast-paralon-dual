#!/usr/bin/env python3
"""Run GPU machines on Vast.ai and Paralon Cloud at once, or switch between them.

    python3 vast_paralon_dual.py init        # pair machines, write the config
    python3 vast_paralon_dual.py status      # both sides; what it would do
    python3 vast_paralon_dual.py mode dual [VAST_MACHINE_ID]
    python3 vast_paralon_dual.py run         # the watcher (the service)
    python3 vast_paralon_dual.py service     # print the systemd unit
"""

import sys

from vpdual.cli import main

if __name__ == "__main__":
    sys.exit(main())

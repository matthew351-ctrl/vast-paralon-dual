#!/usr/bin/env python3
"""Run one GPU machine on Vast.ai and Paralon Cloud, or switch between them.

    python3 vast_paralon_dual.py -c config.json mode dual
    python3 vast_paralon_dual.py -c config.json run
    python3 vast_paralon_dual.py -c config.json status
"""

import sys

from vpdual.cli import main

if __name__ == "__main__":
    sys.exit(main())

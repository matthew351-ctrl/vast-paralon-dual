"""`python -m vpdual`: what a pip / pipx install's `service` unit runs."""

import sys

from .cli import main

sys.exit(main())

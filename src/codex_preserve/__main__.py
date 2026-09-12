"""``python -m codex_preserve`` runs the same command as ``codex-preserve``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

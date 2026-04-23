"""PyInstaller entry point for the brainloopd binary.

Kept outside the `daemon/` package so PyInstaller treats it as a
standalone script — `from daemon.daemon import main` then works as an
absolute import in both the frozen binary and during source development.
"""

from __future__ import annotations

import sys

from daemon.daemon import main


if __name__ == "__main__":
    sys.exit(main() or 0)

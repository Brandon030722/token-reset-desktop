"""PyInstaller CLI entry: no Tk, browser, or resident HTTP server."""
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor.runner import main

if __name__ == "__main__":
    raise SystemExit(main())

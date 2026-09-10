"""Frozen one-shot worker: no Tk, HTTP server, or GUI dependencies."""
from monitor.runner import main

if __name__ == "__main__":
    raise SystemExit(main())

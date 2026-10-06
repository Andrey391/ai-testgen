"""Where the studio keeps its files (a module of its own: everything imports it).

The data lives in PostgreSQL only (db.py, fs.py). DATA and SECRETS are the roots of its keys
("data/projects/<id>/project.json" is the path DATA/projects/<id>/project.json) and, on the local
disk, a cache: the browser reads and writes screenshots, traces and files for upload steps there,
fs.push() sends them to the database. Losing the cache loses nothing.

TESTGEN_CACHE_DIR moves the cache (by default %LOCALAPPDATA%\\aitestgen\\cache on Windows,
$XDG_CACHE_HOME/aitestgen or ~/.cache/aitestgen elsewhere) - outside the code, so worktrees and
checkouts of the studio share it.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _home() -> Path:
    """The studio's folder of this user: the cache and the key of local development (vault.py)."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "aitestgen"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "aitestgen"


HOME = _home()
CACHE = Path(os.environ.get("TESTGEN_CACHE_DIR") or HOME / "cache")
DATA = CACHE / "data"
SECRETS = CACHE / "secrets"
OFFLINE = os.environ.get("TESTGEN_OFFLINE", "off").lower() in ("on", "1", "true", "yes")


def utf8_console() -> None:
    """Command-line tools: Russian messages into a file or a pipe (systemd, docker, CI) - on Windows
    that is cp1252 otherwise."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

"""Where the studio keeps its files (a module of its own: everything imports it).

TESTGEN_DATA_DIR moves the data folder, e.g. to a directory versioned with the
application so CI runs the same tests (python -m testgen.run); TESTGEN_SECRETS_DIR
moves the secrets (CI, the studio's own tests).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("TESTGEN_DATA_DIR") or ROOT / "data")
SECRETS = Path(os.environ.get("TESTGEN_SECRETS_DIR") or ROOT / "secrets")
OFFLINE = os.environ.get("TESTGEN_OFFLINE", "off").lower() in ("on", "1", "true", "yes")

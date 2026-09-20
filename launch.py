#!/usr/bin/env python3
"""Load a pinned, unmodified upstream KlipperScreen with our extension class."""
import os
import sys
from pathlib import Path
from jogger.config import Store

BASE = Path(__file__).resolve().parent
UPSTREAM = Path(os.environ.get("KDJ_KLIPPERSCREEN", "~/.local/share/klipper-dashboard-jogger/KlipperScreen")).expanduser()
if not (UPSTREAM / "screen.py").exists():
    raise SystemExit("KlipperScreen is missing. Run: bash scripts/install.sh")
sys.path.insert(0, str(UPSTREAM))
os.chdir(UPSTREAM)
import screen as upstream
from jogger.integration import make_window

store = Store()
from jogger.privacy import redact_logs
redact_logs(store.printers)
config = store.generate()
upstream.KlipperScreen = make_window(upstream.KlipperScreen, store, BASE)
if "-c" not in sys.argv and "--configfile" not in sys.argv:
    sys.argv.extend(["-c", str(config)])
upstream.main()

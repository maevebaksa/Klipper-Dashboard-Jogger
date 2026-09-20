#!/usr/bin/env bash
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
xset s off -dpms || true
exec "$KDJ_PYTHON" "$SOURCE/launch.py"

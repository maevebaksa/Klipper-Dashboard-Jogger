#!/usr/bin/env bash
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export KDJ_PYTHON="${KDJ_PYTHON:-$HOME/.local/share/klipper-dashboard-jogger/venv/bin/python}"
exec /usr/bin/xinit /bin/bash "$SOURCE/scripts/xclient.sh" -- :0 vt7 -nolisten tcp

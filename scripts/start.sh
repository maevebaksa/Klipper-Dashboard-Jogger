#!/usr/bin/env bash
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export KDJ_PYTHON="${KDJ_PYTHON:-$HOME/.local/share/klipper-dashboard-jogger/venv/bin/python}"
# Let Xorg/logind choose and own the VT from the systemd session.  Passing an
# explicit vt7 here can race logind and yield a paused DRM fd on newer Pi OS.
exec /usr/bin/xinit /bin/bash "$SOURCE/scripts/xclient.sh"

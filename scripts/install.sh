#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID == 0 ]]; then
    echo 'Run as your normal Pi user (without sudo). The installer requests sudo when needed.' >&2
    exit 1
fi
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
case "$SOURCE" in *[[:space:]]*) echo 'Clone into a path without spaces.' >&2; exit 1;; esac
source /etc/os-release
if [[ ${ID:-} != raspbian && ${ID:-} != debian && ${ID_LIKE:-} != *debian* ]]; then
    echo 'This installer targets Raspberry Pi OS / Debian 12 or newer.' >&2; exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Use Raspberry Pi OS Bookworm or newer"'
KDJ_DATA="$HOME/.local/share/klipper-dashboard-jogger"
KDJ_BASE="$KDJ_DATA/KlipperScreen"
KDJ_ENV="$KDJ_DATA/venv"
sudo apt-get update
sudo apt-get install -y git python3-venv python3-dev python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-3.0 librsvg2-common libmpv-dev libsystemd-dev build-essential pkg-config \
    libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 \
    xinit xserver-xorg xserver-xorg-legacy x11-xserver-utils xinput dbus-x11 \
    fonts-dejavu avahi-daemon libnss-mdns iproute2 kbd
mkdir -p "$KDJ_DATA"
KDJ_REF=$(tr -d '\n' < "$SOURCE/klipperscreen.ref")
if [[ ! -d "$KDJ_BASE/.git" ]]; then
    git clone https://github.com/KlipperScreen/KlipperScreen.git "$KDJ_BASE"
fi
if [[ -n $(git -C "$KDJ_BASE" status --porcelain) ]]; then
    echo "The managed KlipperScreen checkout has local changes: $KDJ_BASE. Preserve them before reinstalling." >&2
    exit 1
fi
git -C "$KDJ_BASE" fetch origin "$KDJ_REF"
git -C "$KDJ_BASE" checkout --detach "$KDJ_REF"
python3 -m venv --system-site-packages "$KDJ_ENV"
"$KDJ_ENV/bin/python" -m pip install --upgrade pip
"$KDJ_ENV/bin/python" -m pip install --only-binary=sdbus -r "$SOURCE/requirements.txt"
"$KDJ_ENV/bin/python" -c 'import gi, cairo, pygame, zeroconf, requests, websocket, sdbus; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
# Limit non-root device access to joystick-class devices, not all input events.
sudo groupadd -f kdj-gamepad
sudo usermod -aG kdj-gamepad,video,render,tty "$USER"
sudo tee /etc/udev/rules.d/70-kdj-gamepad.rules >/dev/null <<'RULES'
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_JOYSTICK}=="1", GROUP="kdj-gamepad", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="input", KERNEL=="js*", GROUP="kdj-gamepad", MODE="0660", TAG+="uaccess"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input
if [[ -f /etc/X11/Xwrapper.config && ! -f /etc/X11/Xwrapper.config.kdj-backup ]]; then
    sudo cp /etc/X11/Xwrapper.config /etc/X11/Xwrapper.config.kdj-backup
fi
sudo tee /etc/X11/Xwrapper.config >/dev/null <<'XCONFIG'
allowed_users=anybody
needs_root_rights=yes
XCONFIG
sudo tee /etc/systemd/system/klipper-dashboard-jogger.service >/dev/null <<UNIT
[Unit]
Description=Klipper Dashboard Jogger
After=systemd-user-sessions.service network.target
Wants=dbus.socket
Conflicts=getty@tty7.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
SupplementaryGroups=kdj-gamepad video render tty
WorkingDirectory=$SOURCE
Environment=KDJ_KLIPPERSCREEN=$KDJ_BASE
Environment=KDJ_PYTHON=$KDJ_ENV/bin/python
ExecStart=/bin/bash $SOURCE/scripts/start.sh
Restart=on-failure
RestartSec=3
PAMName=login
TTYPath=/dev/tty7
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
StandardInput=tty
UtmpIdentifier=tty7
UtmpMode=user

[Install]
WantedBy=multi-user.target
UNIT
# A fresh Lite installation has no competing display manager. Never disable an existing one silently.
sudo systemctl daemon-reload
sudo systemctl enable avahi-daemon klipper-dashboard-jogger.service
if systemctl is-active --quiet display-manager || systemctl is-active --quiet KlipperScreen; then
    echo 'Installed. Another display service is active; stop it before starting klipper-dashboard-jogger.'
else
    sudo systemctl restart klipper-dashboard-jogger
fi
echo 'Ready. Open Gamepad setup and learn a hold-to-jog button before moving a printer.'

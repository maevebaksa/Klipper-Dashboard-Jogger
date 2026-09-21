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
    gir1.2-gtk-3.0 gir1.2-webkit2-4.1 librsvg2-common libmpv-dev libsystemd-dev build-essential pkg-config \
    libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 \
    xinit xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-legacy x11-xserver-utils xinput dbus-x11 \
    fonts-dejavu avahi-daemon libnss-mdns iproute2 kbd network-manager polkitd
# On modern Raspberry Pi HDMI/KMS systems the legacy fbdev Xorg driver can
# claim fb0 as Screen 0 and demote vc4/modesetting to G0, which can make
# Xorg abort before KlipperScreen starts. The modesetting driver is built
# into xserver-xorg-core and is the correct backend for vc4 KMS.
if [[ -r /proc/device-tree/model ]] && grep -qa 'Raspberry Pi' /proc/device-tree/model; then
    if dpkg-query -W -f='${Status}' xserver-xorg-video-fbdev 2>/dev/null | grep -q 'install ok installed'; then
        sudo apt-get remove -y xserver-xorg-video-fbdev
    fi
fi
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
"$KDJ_ENV/bin/python" -c 'import gi, cairo, pygame, zeroconf, requests, websocket, sdbus; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1"); from gi.repository import Gtk, WebKit2'
# Limit non-root device access to joystick-class devices, not all input events.
sudo groupadd -f kdj-gamepad
sudo groupadd -f network
sudo groupadd -f netdev
sudo usermod -aG kdj-gamepad,video,render,tty,network,netdev "$USER"
sudo tee /etc/udev/rules.d/70-kdj-gamepad.rules >/dev/null <<'RULES'
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_JOYSTICK}=="1", GROUP="kdj-gamepad", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="input", KERNEL=="js*", GROUP="kdj-gamepad", MODE="0660", TAG+="uaccess"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input

# Allow the built-in KlipperScreen Network panel to manage NetworkManager from
# the appliance UI without prompting for an unavailable desktop password dialog.
sudo mkdir -p /etc/polkit-1/rules.d
sudo tee /etc/polkit-1/rules.d/90-klippercontroller-network.rules >/dev/null <<'RULES'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 &&
        subject.isInGroup("network")) {
        return polkit.Result.YES;
    }
});
RULES
if [[ -f /etc/X11/Xwrapper.config && ! -f /etc/X11/Xwrapper.config.kdj-backup ]]; then
    sudo cp /etc/X11/Xwrapper.config /etc/X11/Xwrapper.config.kdj-backup
fi
sudo tee /etc/X11/Xwrapper.config >/dev/null <<'XCONFIG'
allowed_users=anybody
needs_root_rights=yes
XCONFIG

# Raspberry Pi OS can expose both fbdev and vc4 DRM to Xorg.  On some
# Bookworm/Trixie Lite installs Xorg otherwise selects FBDEV as screen 0
# and leaves vc4/modesetting as a secondary GPU, which can abort at startup.
# Match only the vc4 driver so this is harmless on non-Pi Debian systems.
sudo mkdir -p /etc/X11/xorg.conf.d
sudo tee /etc/X11/xorg.conf.d/99-v3d.conf >/dev/null <<'XCONFIG'
Section "OutputClass"
    Identifier "vc4"
    MatchDriver "vc4"
    Driver "modesetting"
    Option "PrimaryGPU" "true"
EndSection
XCONFIG
sudo tee /etc/systemd/system/klipper-dashboard-jogger.service >/dev/null <<UNIT
[Unit]
Description=Klipper Dashboard Jogger
After=systemd-user-sessions.service systemd-logind.service dbus.socket network.target
Wants=systemd-logind.service dbus.socket
ConditionPathExists=/dev/tty0
Conflicts=getty@tty7.service
StartLimitIntervalSec=30
StartLimitBurst=3

[Service]
Type=simple
User=$USER
SupplementaryGroups=kdj-gamepad video render tty network netdev
WorkingDirectory=$SOURCE
Environment=KDJ_KLIPPERSCREEN=$KDJ_BASE
Environment=KDJ_PYTHON=$KDJ_ENV/bin/python
ExecStart=/bin/bash $SOURCE/scripts/start.sh
# Xorg chooses a free VT on current Raspberry Pi OS. Switch to the VT it
# actually chose instead of assuming tty7.
ExecStartPost=+/bin/bash $SOURCE/scripts/activate-x-vt.sh
Restart=on-failure
RestartSec=3
PAMName=%u
TTYPath=/dev/tty7
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
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

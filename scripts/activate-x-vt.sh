#!/usr/bin/env bash
set -euo pipefail

# xinit may take a moment to spawn Xorg.  Do not assume a fixed VT: on recent
# Raspberry Pi OS/logind combinations Xorg can legitimately choose tty2, tty3,
# etc.  Switching to a hard-coded tty7 previously hid a healthy X session.
for _ in $(seq 1 60); do
    tty="$(
        ps -eo tty=,args= |
        awk '$0 ~ /\/Xorg :0([[:space:]]|$)/ && $1 ~ /^tty[0-9]+$/ {print $1; exit}'
    )"
    if [[ "$tty" =~ ^tty([0-9]+)$ ]]; then
        /usr/bin/chvt "${BASH_REMATCH[1]}"
        exit 0
    fi
    sleep 0.1
done

echo "KlipperController: Xorg :0 did not expose a console VT in time" >&2
exit 0

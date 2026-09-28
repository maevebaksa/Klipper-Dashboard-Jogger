"""Klipper Filament Sync integration. No GTK here.

The plugin (github.com/maevebaksa/Klipper-Filament-Sync) runs on each printer:
a SET_TOOL_FILAMENT macro stores material, color and temperatures per tool,
and a bridge mirrors them into Moonraker's "lane_data" database namespace as
{"tool0": {"lane": "0", "material": "PLA", "color": "FF0000", ...}, ...}.

KlipperController uses it two ways:

* The plugin's own KlipperScreen panel (klipperscreen/maeve_filaments.py),
  loaded from a checkout pinned in filament-sync.ref, in each printer's main
  menu when that printer has the macro.
* Dashboard tiles show each tool's filament, read from lane_data.
"""
import os
import re

PANEL = "maeve_filaments"
MACRO = "SET_TOOL_FILAMENT"
CHECKOUT = "Klipper-Filament-Sync"  # directory in the app data directory
PANEL_FILE = os.path.join("klipperscreen", "maeve_filaments.py")
# Same values and limits as the plugin's panel.
EMPTY_MATERIALS = {"", "EMPTY", "NONE", "UNKNOWN", "UNSET"}
MAX_TOOLS = 32
_LANE_KEY = re.compile(r"^tool(\d+)$", re.IGNORECASE)

# KlipperScreen menu entry, as the plugin's klipperscreen/menu.conf, plus an
# enable condition so printers without the plugin do not show it. Macro names
# are compared lowercased because Klipper may report section names in either
# case.
MENU_SECTION = "menu __main tool_filaments"
MENU_ITEM = {
    "name": "Tool Filaments",
    "icon": "spool",
    "style": "color2",
    "panel": PANEL,
    "enable": "{{ '%s' in printer.gcode_macros.list | map('lower') | list }}" % MACRO.lower(),
}


def panel_path(data_dir):
    return os.path.join(str(data_dir), CHECKOUT, PANEL_FILE)


def normalize_color(value):
    """RRGGBB in upper case, or 000000 for anything unreadable (as the plugin does)."""
    color = str(value or "000000").strip().upper()
    if color.startswith("#"):
        color = color[1:]
    if color.startswith("0X"):
        color = color[2:]
    if len(color) == 8:
        color = color[:6]
    if len(color) != 6 or any(c not in "0123456789ABCDEF" for c in color):
        return "000000"
    return color


def parse_lanes(value):
    """lane_data namespace value -> [{"tool", "material", "color"}], by tool number.

    Empty tools keep their place with material "".
    """
    if not isinstance(value, dict):
        return []
    lanes = []
    for key, lane in value.items():
        match = _LANE_KEY.fullmatch(str(key))
        if not match or int(match.group(1)) >= MAX_TOOLS or not isinstance(lane, dict):
            continue
        material = str(lane.get("material") or "").strip().upper()
        if material in EMPTY_MATERIALS:
            material = ""
        lanes.append({
            "tool": int(match.group(1)),
            "material": material,
            "color": normalize_color(lane.get("color")) if material else "",
        })
    return sorted(lanes, key=lambda lane: lane["tool"])


def summary(lanes):
    """Short text for a tile: 'PLA', 'PLA · PETG', '3/4 loaded' or 'Empty'."""
    loaded = [lane for lane in lanes if lane["material"]]
    if not lanes:
        return ""
    if not loaded:
        return "Empty"
    if len(lanes) <= 2:
        return " · ".join(lane["material"] or "Empty" for lane in lanes)
    return f"{len(loaded)}/{len(lanes)} loaded"

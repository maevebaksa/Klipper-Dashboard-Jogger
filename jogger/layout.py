"""Dashboard grid sizing. No GTK here, so it is testable.

The dashboard must show at least nine printers without scrolling on the
screens this runs on (1280x800 and 1024x600 Waveshare panels, minus
KlipperScreen's title bar and action bar). Rather than assume a size, the
grid is fitted to the space the dashboard actually gets.
"""
import math

GAP = 10            # FlowBox row and column spacing, px
MIN_TILE_W = 170    # narrowest tile that still fits "Printing 100%" in large type
MIN_TILE_H = 92     # shortest tile that still shows name, state and route
MAX_TILE_H = 170    # taller tiles only add empty space
IDEAL_ASPECT = 1.8  # width / height that reads best at a glance
FULL_DETAIL_H = 124  # below this the file-name line is hidden


def grid(count, width, height):
    """Return (columns, tile_height) for count tiles in a width x height area.

    Picks the column count whose tiles are closest to IDEAL_ASPECT among the
    layouts where every tile fits without scrolling. If none fits, uses as
    many columns as the width allows at the minimum height (the area scrolls).
    """
    count = max(1, count)
    max_cols = max(1, min(count, (width + GAP) // (MIN_TILE_W + GAP)))
    best = None
    for cols in range(1, max_cols + 1):
        rows = math.ceil(count / cols)
        tile_w = (width - (cols - 1) * GAP) / cols
        room_h = (height - (rows - 1) * GAP) / rows
        if room_h < MIN_TILE_H:
            continue
        tile_h = min(room_h, MAX_TILE_H)
        score = abs(math.log((tile_w / tile_h) / IDEAL_ASPECT))
        if best is None or score < best[0]:
            best = (score, cols, int(tile_h))
    if best is None:
        return max_cols, MIN_TILE_H
    return best[1], best[2]


def fits(count, width, height):
    cols, tile_h = grid(count, width, height)
    rows = math.ceil(max(1, count) / cols)
    return rows * tile_h + (rows - 1) * GAP <= height and \
        cols * MIN_TILE_W + (cols - 1) * GAP <= width

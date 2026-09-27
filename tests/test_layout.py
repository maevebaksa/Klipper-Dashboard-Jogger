import pytest

from jogger.layout import GAP, MAX_TILE_H, MIN_TILE_H, fits, grid


# Content areas left after KlipperScreen's title bar and action bar, roughly,
# for common Waveshare panels, plus a generous and a tight case.
SCREENS = [(1180, 690), (1180, 600), (924, 500), (700, 380), (1860, 960)]


@pytest.mark.parametrize("width, height", SCREENS)
@pytest.mark.parametrize("count", range(1, 10))
def test_up_to_nine_printers_fit_without_scrolling(count, width, height):
    assert fits(count, width, height), grid(count, width, height)


def test_tiles_never_exceed_their_height_bounds():
    for count in range(1, 13):
        _cols, tile_h = grid(count, 1180, 690)
        assert MIN_TILE_H <= tile_h <= MAX_TILE_H


def test_nine_printers_use_a_balanced_grid_on_a_ten_inch_panel():
    cols, tile_h = grid(9, 1180, 690)
    assert cols in (3, 4, 5)
    rows = -(-9 // cols)
    assert rows * tile_h + (rows - 1) * GAP <= 690


def test_too_many_printers_fall_back_to_scrolling():
    cols, tile_h = grid(60, 700, 380)
    assert tile_h == MIN_TILE_H and cols >= 3
    assert not fits(60, 700, 380)

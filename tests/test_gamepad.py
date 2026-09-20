import pytest
from jogger.gamepad import button_jog_vector, normalize


def test_deadzone_and_inversion():
    assert normalize(.15, .22) == 0
    assert normalize(0, .22, True) == 0
    assert normalize(1, .22) == 1
    assert normalize(-1, .22, True) == 1
    assert normalize(.61, .22) == pytest.approx(.5)


def test_button_jog_vector_and_opposite_cancel():
    settings = {
        "buttons": {
            "1": "jog:x+",
            "2": "jog:x-",
            "3": "jog:z-",
            "4": "pause",
        }
    }
    assert button_jog_vector(settings, {1}) == (1.0, 0.0, 0.0)
    assert button_jog_vector(settings, {1, 2}) == (0.0, 0.0, 0.0)
    assert button_jog_vector(settings, {3, 4}) == (0.0, 0.0, -1.0)

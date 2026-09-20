import pytest
from jogger.gamepad import normalize


def test_deadzone_and_inversion():
    assert normalize(.15, .22) == 0
    assert normalize(0, .22, True) == 0
    assert normalize(1, .22) == 1
    assert normalize(-1, .22, True) == 1
    assert normalize(.61, .22) == pytest.approx(.5)

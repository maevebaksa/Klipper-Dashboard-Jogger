import copy
import threading
import time
import pytest
from jogger.motion import Motion, jog_script

READY = {
    "webhooks": {"state": "ready"}, "print_stats": {"state": "standby"},
    "pause_resume": {"is_paused": False}, "virtual_sdcard": {"is_active": False},
    "gcode_move": {"absolute_coordinates": False, "gcode_position": [50, 50, 50, 0], "speed_factor": 1},
    "toolhead": {"homed_axes": "xyz", "position": [50, 50, 50, 0],
                 "axis_minimum": [0, 0, 0], "axis_maximum": [100, 100, 100]},
}


@pytest.mark.parametrize("change", [
    {"webhooks": {"state": "shutdown"}}, {"print_stats": {"state": "printing"}},
    {"print_stats": {"state": "paused"}}, {"print_stats": {}},
    {"pause_resume": {"is_paused": True}}, {"virtual_sdcard": {"is_active": True}},
    {"toolhead": {"homed_axes": "xy"}},
])
def test_unready_never_moves(change):
    state = copy.deepcopy(READY)
    state.update(change)
    with pytest.raises(ValueError):
        jog_script(state, (1, 0, 0))


def test_move_step_speed_and_parser_restore():
    text = jog_script(READY, (1, 0.5, 0), step=5, speed_xy=50, speed_z=10)
    assert "G1 X5.0000 F3000.000" in text
    assert "G1 Y" not in text
    assert "M400\nRESTORE_GCODE_STATE NAME=KDJ_JOG MOVE=0" in text
    # Remote motion uses the selected step too, but Motion only permits one
    # remote request per centered deflection.
    assert "X5.0000" in jog_script(READY, (1, 0, 0), True, step=5)
    assert "Z-0.5000 F600.000" in jog_script(
        READY, (0, 0, -1), step=.5, speed_z=10
    )


def test_speed_ramp_and_stick_magnitude_never_exceed_move_speed():
    slow = jog_script(READY, (1, 0, 0), speed_xy=50, speed_scale=.35)
    fast = jog_script(READY, (1, 0, 0), speed_xy=50, speed_scale=1)
    half = jog_script(READY, (.5, 0, 0), speed_xy=50, speed_scale=1)
    assert "F1050.000" in slow
    assert "F3000.000" in fast
    assert "F1500.000" in half


def test_limit_and_invalid_input():
    state = copy.deepcopy(READY)
    state["toolhead"]["position"][0] = 100
    for vector in ((1, 0, 0), (float("nan"), 0, 0), (2, 0, 0)):
        with pytest.raises(ValueError):
            jog_script(state, vector)


def test_absolute_coordinates_and_speed_factor_preserved():
    state = copy.deepcopy(READY)
    state["gcode_move"].update(absolute_coordinates=True, speed_factor=2)
    script = jog_script(state, (1, 0, 0))
    assert "G1 X51.0000 F1500.000" in script
    assert "G90" not in script and "G91" not in script


class FakeClient:
    remote = False

    def __init__(self):
        self.moves = []
        self.query_started = threading.Event()
        self.query_release = threading.Event()
        self.query_release.set()

    def status(self):
        self.query_started.set()
        assert self.query_release.wait(2)
        return READY

    def gcode(self, script):
        self.moves.append(script)


def arm(motion):
    motion.sample(False, (0, 0, 0), True)
    motion.sample(True, (0, 0, 0), True)
    motion.sample(True, (1, 0, 0), True)


def wait(motion):
    end = time.monotonic() + 2
    while motion.busy and time.monotonic() < end:
        time.sleep(.005)
    assert not motion.busy


def test_held_on_attach_and_deflected_press_do_not_arm():
    motion = Motion()
    client = FakeClient()
    motion.reset(client)
    motion.sample(True, (1, 0, 0), True)
    motion.tick()
    assert not client.moves
    motion.sample(False, (1, 0, 0), True)
    motion.sample(True, (1, 0, 0), True)
    assert not motion.armed
    arm(motion)
    motion.tick()
    wait(motion)
    assert len(client.moves) == 1


@pytest.mark.parametrize("interrupt", ["release", "switch", "focus", "stale"])
def test_inflight_query_cannot_move_after_interrupt(interrupt):
    motion = Motion()
    client = FakeClient()
    client.query_release.clear()
    motion.reset(client)
    arm(motion)
    motion.tick()
    assert client.query_started.wait(1)
    if interrupt == "release":
        motion.sample(False, (0, 0, 0), True)
    elif interrupt == "switch":
        motion.reset(FakeClient())
    elif interrupt == "focus":
        motion.sample(True, (1, 0, 0), False)
    else:
        motion.last_input = 0
    client.query_release.set()
    wait(motion)
    assert client.moves == []


def test_remote_only_one_move_until_stick_centered():
    motion = Motion()
    client = FakeClient()
    client.remote = True
    motion.reset(client)
    arm(motion)
    motion.tick()
    wait(motion)
    motion.tick()
    assert len(client.moves) == 1
    motion.sample(True, (0, 0, 0), True)
    motion.sample(True, (1, 0, 0), True)
    motion.tick()
    wait(motion)
    assert len(client.moves) == 2


def test_timeout_never_retries_and_requires_rearm():
    class TimeoutClient(FakeClient):
        def gcode(self, script):
            super().gcode(script)
            raise RuntimeError("timeout")
    messages = []
    motion = Motion(messages.append)
    client = TimeoutClient()
    motion.reset(client)
    arm(motion)
    motion.tick()
    wait(motion)
    motion.tick()
    assert len(client.moves) == 1
    assert not motion.armed
    assert messages == ["timeout"]

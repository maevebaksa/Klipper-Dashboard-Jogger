"""Bounded jogging: fresh state, release-to-arm, one request, no retry."""
import math
import threading
import time


def jog_script(status, vector, remote=False):
    if status.get("webhooks", {}).get("state") != "ready":
        raise ValueError("Klipper is not ready")
    if status.get("print_stats", {}).get("state") not in ("standby", "complete", "cancelled", "error"):
        raise ValueError("Jogging is disabled during printing or pause")
    if status.get("virtual_sdcard", {}).get("is_active") or status.get("pause_resume", {}).get("is_paused"):
        raise ValueError("Printer is printing or paused")
    tool = status.get("toolhead", {})
    if not all(axis in tool.get("homed_axes", "") for axis in "xyz"):
        raise ValueError("Home all axes before jogging")
    if len(vector) != 3 or any(not math.isfinite(v) or abs(v) > 1 for v in vector):
        raise ValueError("Invalid joystick input")
    # Dominant axis only. Avoid diagonal Z/XY motion and unbounded queues.
    i = max(range(3), key=lambda n: abs(vector[n]))
    if abs(vector[i]) < 0.01:
        return None
    distance = (0.2 if remote else (0.2 if i == 2 else 1.0)) * vector[i]
    pos = tool.get("position", [])
    low, high = tool.get("axis_minimum", []), tool.get("axis_maximum", [])
    if min(len(pos), len(low), len(high)) < 3:
        raise ValueError("Waiting for axis limits")
    if not low[i] <= pos[i] + distance <= high[i]:
        raise ValueError("Axis travel limit reached")
    # Never switch G90/G91: a rejected G1 aborts the remainder of a script.
    # Use the current coordinate mode and its transformed gcode position instead.
    move = status.get("gcode_move", {})
    if not isinstance(move.get("absolute_coordinates"), bool):
        raise ValueError("Waiting for coordinate mode")
    target = distance
    if move["absolute_coordinates"]:
        position = move.get("gcode_position", [])
        if len(position) < 3 or not math.isfinite(position[i]):
            raise ValueError("Waiting for G-code position")
        target += position[i]
    factor = move.get("speed_factor", 0)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("Invalid speed factor")
    # Compensate M220 so the physical feed never exceeds our cap.
    speed = (180 if i == 2 else 600) / factor
    return ("SAVE_GCODE_STATE NAME=KDJ_JOG\n"
            f"G1 {'XYZ'[i]}{target:.4f} F{speed:.3f}\nM400\n"
            "RESTORE_GCODE_STATE NAME=KDJ_JOG MOVE=0")


class Motion:
    def __init__(self, report=lambda text: None):
        self.lock = threading.Lock()
        self.report = report
        self.client = None
        self.epoch = 0
        self.armed = False
        self.released = False
        self.held = False
        self.vector = (0., 0., 0.)
        self.last_input = 0.
        self.busy = False
        self.remote_latch = False
        self.enabled = False

    def reset(self, client=None):
        with self.lock:
            self.epoch += 1
            self.client = client
            self.armed = self.released = self.held = self.enabled = False
            self.vector = (0., 0., 0.)
            self.remote_latch = False

    def disarm(self):
        with self.lock:
            self.epoch += 1
            self.armed = self.released = self.held = False

    def sample(self, held, vector, allowed, now=None):
        now = time.monotonic() if now is None else now
        vector = tuple(vector)
        with self.lock:
            self.last_input = now
            self.held, self.vector, self.enabled = bool(held), vector, bool(allowed)
            centered = all(abs(v) < .01 for v in vector)
            if not allowed:
                self.armed = self.released = False
                self.epoch += 1
                return
            if not held:
                self.armed = False
                self.released = centered
                self.epoch += 1
            elif not self.armed and self.released and centered:
                self.armed = True
            if centered:
                self.remote_latch = False

    def valid(self, epoch):
        return (epoch == self.epoch and self.armed and self.held and self.enabled and
                time.monotonic() - self.last_input < .25)

    def tick(self):
        with self.lock:
            if (self.busy or not self.client or not self.valid(self.epoch) or
                    not any(self.vector) or (self.client.remote and self.remote_latch)):
                return
            self.busy = True
            epoch, client, vector = self.epoch, self.client, self.vector
            self.remote_latch = True
        threading.Thread(target=self._move, args=(epoch, client, vector), daemon=True).start()

    def _move(self, epoch, client, vector):
        try:
            status = client.status()
            script = jog_script(status, vector, client.remote)
            # Check the live input again after network I/O, before sending any motion.
            with self.lock:
                axis = max(range(3), key=lambda n: abs(vector[n]))
                valid = (self.valid(epoch) and self.vector[axis] * vector[axis] > 0 and
                         axis == max(range(3), key=lambda n: abs(self.vector[n])))
            if valid and script:
                client.gcode(script)
        except Exception as exc:
            with self.lock:
                current = self.client is client
            if current:
                self.disarm()
                self.report(str(exc))
        finally:
            with self.lock:
                self.busy = False

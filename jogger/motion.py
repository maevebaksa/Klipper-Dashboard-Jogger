"""Bounded jogging: fresh state, release-to-arm, one request, no retry."""
import math
import threading
import time


RAMP_SECONDS = 1.5
RAMP_START = 0.35


def jog_script(status, vector, remote=False, step=1.0, speed_xy=50.0, speed_z=10.0,
               speed_scale=1.0):
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
    if not math.isfinite(step) or step <= 0:
        raise ValueError("Invalid move distance")
    if not math.isfinite(speed_xy) or speed_xy <= 0 or not math.isfinite(speed_z) or speed_z <= 0:
        raise ValueError("Invalid move speed")
    if not math.isfinite(speed_scale) or not 0 < speed_scale <= 1:
        raise ValueError("Invalid jog speed scale")

    # Dominant axis only. Avoid diagonal Z/XY motion and unbounded queues.
    i = max(range(3), key=lambda n: abs(vector[n]))
    magnitude = abs(vector[i])
    if magnitude < 0.01:
        return None

    # Match the exact distance selected in KlipperScreen's Move panel. Remote
    # control remains discrete: Motion.remote_latch requires centering before
    # another step can be sent.
    distance = math.copysign(float(step), vector[i])

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

    # The Move panel's speed is the hard cap. Stick deflection controls speed,
    # and Motion ramps from RAMP_START to full configured speed while held.
    configured = float(speed_z if i == 2 else speed_xy)
    input_scale = max(0.20, magnitude)
    physical_speed = max(1.0, configured * input_scale * speed_scale)
    physical_speed = min(configured, physical_speed)
    feed = (physical_speed * 60.0) / factor

    return ("SAVE_GCODE_STATE NAME=KDJ_JOG\n"
            f"G1 {'XYZ'[i]}{target:.4f} F{feed:.3f}\nM400\n"
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
        self.step = 1.0
        self.speed_xy = 50.0
        self.speed_z = 10.0
        self.motion_started = None
        self.motion_direction = None

    def reset(self, client=None):
        with self.lock:
            self.epoch += 1
            self.client = client
            self.armed = self.released = self.held = self.enabled = False
            self.vector = (0., 0., 0.)
            self.remote_latch = False
            self.motion_started = None
            self.motion_direction = None

    def disarm(self):
        with self.lock:
            self.epoch += 1
            self.armed = self.released = self.held = False
            self.motion_started = None
            self.motion_direction = None

    @staticmethod
    def _direction(vector):
        if not any(abs(v) >= .01 for v in vector):
            return None
        axis = max(range(3), key=lambda n: abs(vector[n]))
        return axis, 1 if vector[axis] > 0 else -1

    def sample(self, held, vector, allowed, now=None, step=1.0, speed_xy=50.0, speed_z=10.0):
        now = time.monotonic() if now is None else now
        vector = tuple(vector)
        with self.lock:
            self.last_input = now
            self.held, self.vector, self.enabled = bool(held), vector, bool(allowed)
            self.step = float(step)
            self.speed_xy = float(speed_xy)
            self.speed_z = float(speed_z)
            centered = all(abs(v) < .01 for v in vector)

            if not allowed:
                self.armed = self.released = False
                self.motion_started = None
                self.motion_direction = None
                self.epoch += 1
                return

            if not held:
                self.armed = False
                self.released = centered
                self.motion_started = None
                self.motion_direction = None
                self.epoch += 1
            elif not self.armed and self.released and centered:
                self.armed = True

            direction = self._direction(vector) if self.armed and held else None
            if direction is None:
                self.motion_started = None
                self.motion_direction = None
            elif direction != self.motion_direction:
                self.motion_direction = direction
                self.motion_started = now

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
            step, speed_xy, speed_z = self.step, self.speed_xy, self.speed_z
            held_for = 0.0 if self.motion_started is None else max(0.0, time.monotonic() - self.motion_started)
            speed_scale = RAMP_START + (1.0 - RAMP_START) * min(1.0, held_for / RAMP_SECONDS)
            self.remote_latch = True
        threading.Thread(
            target=self._move,
            args=(epoch, client, vector, step, speed_xy, speed_z, speed_scale),
            daemon=True,
        ).start()

    def _move(self, epoch, client, vector, step, speed_xy, speed_z, speed_scale):
        try:
            status = client.status()
            script = jog_script(
                status, vector, client.remote, step=step,
                speed_xy=speed_xy, speed_z=speed_z, speed_scale=speed_scale,
            )
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

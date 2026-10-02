"""Gamepad jogging: fresh state, release-to-arm, no retry.

Local jogging streams short moves while the direction is held, so the toolhead
moves continuously. Remote jogging stays discrete: one Move-panel step per
deflection.
"""
import math
import threading
import time


RAMP_SECONDS = 1.5
RAMP_START = 0.2

# Each streamed move lasts this long at the current speed. Short moves let the
# speed follow the stick; Klipper joins consecutive moves without stopping.
SEGMENT_S = 0.1
# Keep about this much motion queued in Klipper while a direction is held.
# Klipper (klippy/toolhead.py) only moves continuously once it has queued more
# than its step generation look-ahead (BGFLUSH_SG_HIGH_TIME, 0.7 s, in
# extras/motion_queuing.py): with less, it stops the toolhead at the end of
# every chunk. It must stay below BUFFER_TIME_HIGH (1.0 s), where Klipper holds
# the G-code request, so a request never waits on queued motion. This is also
# roughly how far the toolhead coasts after the control is released.
TARGET_LEAD_S = 0.9
# While enough motion is queued, check again this often.
LEAD_POLL_S = 0.05


def queued_lead(status):
    """Seconds of motion Klipper has scheduled ahead of now, or None if unknown."""
    tool = status.get("toolhead", {})
    queued, now = tool.get("print_time"), tool.get("estimated_print_time")
    if not isinstance(queued, (int, float)) or not isinstance(now, (int, float)):
        return None
    if not math.isfinite(queued) or not math.isfinite(now):
        return None
    return queued - now


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

    # Dominant axis only. Avoid diagonal Z/XY motion.
    i = max(range(3), key=lambda n: abs(vector[n]))
    magnitude = abs(vector[i])
    if magnitude < 0.01:
        return None

    move = status.get("gcode_move", {})
    factor = move.get("speed_factor", 0)
    if not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
        raise ValueError("Invalid speed factor")

    # The Move panel's speed is the cap. Stick deflection scales speed below
    # it, and Motion ramps from RAMP_START to full speed while held.
    configured = float(speed_z if i == 2 else speed_xy)
    max_velocity = tool.get("max_velocity")
    if isinstance(max_velocity, (int, float)) and math.isfinite(max_velocity) and max_velocity > 0:
        configured = min(configured, float(max_velocity))
    physical_speed = configured * max(0.20, magnitude) * speed_scale
    physical_speed = min(configured, max(1.0, physical_speed))

    # Remote control sends one Move-panel step per deflection. Local control
    # streams short moves, each SEGMENT_S long at the current speed.
    length = float(step) if remote else physical_speed * SEGMENT_S
    distance = math.copysign(length, vector[i])

    # Stop at the machine's configured travel (position_min / position_max)
    # instead of refusing the move. toolhead.position is the commanded
    # position, so it already includes moves still queued.
    pos = tool.get("position", [])
    low, high = tool.get("axis_minimum", []), tool.get("axis_maximum", [])
    if min(len(pos), len(low), len(high)) < 3:
        raise ValueError("Waiting for axis limits")
    end = min(high[i], max(low[i], pos[i] + distance))
    distance = end - pos[i]
    if abs(distance) < 0.001:
        return None  # Already at the machine limit in this direction.

    # Never switch G90/G91: a rejected G1 aborts the remainder of a script.
    # Use the current coordinate mode and its transformed gcode position instead.
    if not isinstance(move.get("absolute_coordinates"), bool):
        raise ValueError("Waiting for coordinate mode")
    target = distance
    if move["absolute_coordinates"]:
        position = move.get("gcode_position", [])
        if len(position) < 3 or not math.isfinite(position[i]):
            raise ValueError("Waiting for G-code position")
        target += position[i]

    feed = (physical_speed * 60.0) / factor
    # Remote waits for the move (M400) so one request is one finished step.
    # Local must not: waiting would stop the toolhead between moves.
    wait = "M400\n" if remote else ""
    return ("SAVE_GCODE_STATE NAME=KDJ_JOG\n"
            f"G1 {'XYZ'[i]}{target:.4f} F{feed:.3f}\n{wait}"
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
            epoch, client = self.epoch, self.client
            if client.remote:
                self.remote_latch = True
        threading.Thread(target=self._run, args=(epoch, client), daemon=True).start()

    def _snapshot(self, epoch, client):
        """Current input for one move, or None once it no longer allows motion."""
        with self.lock:
            if self.client is not client or not self.valid(epoch) or not any(self.vector):
                return None
            held_for = (0.0 if self.motion_started is None
                        else max(0.0, time.monotonic() - self.motion_started))
            scale = RAMP_START + (1.0 - RAMP_START) * min(1.0, held_for / RAMP_SECONDS)
            return self.vector, self.step, self.speed_xy, self.speed_z, scale

    def _still_wanted(self, epoch, vector):
        """Check the live input again after network I/O, before sending any motion."""
        with self.lock:
            axis = max(range(3), key=lambda n: abs(vector[n]))
            return (self.valid(epoch) and self.vector[axis] * vector[axis] > 0 and
                    axis == max(range(3), key=lambda n: abs(self.vector[n])))

    def _run(self, epoch, client):
        try:
            # Without Klipper's own queue times, assume each move takes its
            # nominal time and track the queue from the wall clock instead.
            queued_until = time.monotonic()
            while True:
                snapshot = self._snapshot(epoch, client)
                if snapshot is None:
                    return
                vector, step, speed_xy, speed_z, scale = snapshot
                status = client.status()
                if not client.remote:
                    lead = queued_lead(status)
                    if lead is None:
                        lead = queued_until - time.monotonic()
                    if lead >= TARGET_LEAD_S:
                        time.sleep(LEAD_POLL_S)
                        continue
                script = jog_script(
                    status, vector, client.remote, step=step,
                    speed_xy=speed_xy, speed_z=speed_z, speed_scale=scale,
                )
                if not self._still_wanted(epoch, vector):
                    return
                if script is None:
                    # At the machine limit: hold here until the input changes.
                    time.sleep(LEAD_POLL_S)
                    continue
                client.gcode(script)
                if client.remote:
                    return
                queued_until = max(queued_until, time.monotonic()) + SEGMENT_S
        except Exception as exc:
            with self.lock:
                current = self.client is client
            if current:
                self.disarm()
                self.report(str(exc))
        finally:
            with self.lock:
                self.busy = False

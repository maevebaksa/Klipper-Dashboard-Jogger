"""SDL HID input. No root/input-group permissions needed with the supplied udev rule."""
import os
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# GTK owns the visible window; SDL has no focused window. The integration layer
# independently gates all inputs against GTK focus, lock and modal state.
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
import pygame

ACTIONS = {
    "none": "No action", "next": "Next printer", "previous": "Previous printer",
    "dashboard": "Printer dashboard", "move": "Move / jog screen",
    "temperature": "Temperature screen", "gcode_macros": "Macros screen",
    "print": "Files / print screen", "pause": "Pause print", "resume": "Resume print (confirm)",
    "cancel": "Cancel print (confirm)", "home": "Home all axes (confirm)",
    "cooldown": "Turn off heaters (confirm)", "estop": "Emergency stop",
}


def normalize(value, deadzone, invert=False):
    value = max(-1., min(1., float(value)))
    if abs(value) <= deadzone:
        return 0.
    scaled = (abs(value) - deadzone) / (1 - deadzone)
    return (-scaled if value < 0 else scaled) * (-1 if invert else 1)


class Gamepad:
    def __init__(self, settings, action, sample, disconnected):
        pygame.display.init()
        pygame.joystick.init()
        self.settings = settings
        self.action = action
        self.sample = sample
        self.disconnected = disconnected
        self.device = None
        self.learn = None
        self.last_buttons = set()
        self.raw_axes = []
        self.name = "No gamepad connected"

    def attach(self):
        wanted = self.settings.get("guid", "")
        for i in range(pygame.joystick.get_count()):
            device = pygame.joystick.Joystick(i)
            if not wanted or device.get_guid() == wanted:
                device.init()
                self.device = device
                self.name = device.get_name()
                self.last_buttons = {b for b in range(device.get_numbuttons()) if device.get_button(b)}
                self.disconnected()  # Attaching never arms motion.
                return

    def poll(self):
        try:
            pygame.event.pump()
            for event in pygame.event.get():
                if (event.type == pygame.JOYDEVICEREMOVED and self.device and
                        event.instance_id == self.device.get_instance_id()):
                    self.device.quit()
                    self.device = None
                    self.name = "Gamepad disconnected"
                    self.disconnected()
            if self.device and not self.device.get_init():
                self.device = None
            if self.device is None:
                self.attach()
            if not self.device:
                return self.sample(False, (0, 0, 0))
            d = self.device
            self.raw_axes = [d.get_axis(i) for i in range(d.get_numaxes())]
            pressed = {i for i in range(d.get_numbuttons()) if d.get_button(i)}
            new = pressed - self.last_buttons
            self.last_buttons = pressed
            if self.learn:
                if new:
                    callback, self.learn = self.learn, None
                    callback(min(new))
                self.sample(False, (0, 0, 0))
                return
            enable = self.settings.get("enable_button")
            for button in sorted(new):
                if button != enable:
                    self.action(self.settings.get("buttons", {}).get(str(button), "none"))
            vector = []
            for axis in "xyz":
                index = self.settings["axes"].get(axis, -1)
                value = self.raw_axes[index] if 0 <= index < len(self.raw_axes) else 0
                vector.append(normalize(value, self.settings["deadzone"], self.settings["invert"].get(axis, False)))
            self.sample(enable is not None and enable in pressed, vector)
        except pygame.error:
            if self.device:
                self.device.quit()
            self.device = None
            self.name = "Gamepad disconnected"
            self.disconnected()

    def close(self):
        self.disconnected()
        pygame.joystick.quit()
        pygame.display.quit()

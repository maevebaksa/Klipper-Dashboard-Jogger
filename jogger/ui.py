"""Touch-friendly dashboard and setup panels hosted inside KlipperScreen."""
import threading
import re
from gi.repository import Gtk, GLib
from ks_includes.screen_panel import ScreenPanel
from .config import profile
from .network import Client, discover
from .gamepad import ACTIONS


def label(text, css=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def button(text, callback, css=None):
    widget = Gtk.Button(label=text)
    widget.set_size_request(-1, 58)
    widget.connect("clicked", lambda _w: callback())
    if css:
        widget.get_style_context().add_class(css)
    return widget


def clear(box):
    for child in box.get_children():
        box.remove(child)


class Dashboard(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Your printers")
        self.content.get_style_context().add_class("kdj")
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=18)
        self.content.add(self.root)
        self.root.pack_start(label("YOUR PRINT SPACE", "kdj-eyebrow"), False, False, 0)
        self.root.pack_start(label("Every printer. One keyboard.", "kdj-heading"), False, False, 0)
        self.root.pack_start(label("Choose a printer to open its controls.  Ctrl + Tab switches printers · F1 returns here", "kdj-muted"), False, False, 0)
        bar = Gtk.Box(spacing=10, homogeneous=True)
        for text, callback in (("Discover printers", self.find), ("Add connection", self.add),
                               ("Gamepad setup", lambda: screen.show_panel("kdj_gamepad"))):
            bar.add(button(text, callback, "kdj-accent"))
        self.root.pack_start(bar, False, False, 0)
        self.status = label("Local network + OctoEverywhere", "kdj-muted")
        self.root.pack_start(self.status, False, False, 0)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll.add(self.cards)
        self.root.pack_start(scroll, True, True, 0)
        self.refresh()

    def refresh(self):
        clear(self.cards)
        store = self._screen.kdj_store
        if not store.printers:
            self.cards.add(label("Welcome aboard.\nDiscover a nearby printer, or add its Moonraker address to get started.", "kdj-empty"))
        for p in store.printers:
            row = Gtk.Box(spacing=10)
            # Never display remote credential-bearing URLs on the dashboard.
            subtitle = "REMOTE · small-step joystick" if p["remote"] else "LOCAL NETWORK"
            row.pack_start(button(p["name"] + "\n" + subtitle, lambda p=p: self._screen.connect_printer(p["name"]), "kdj-card"), True, True, 0)
            row.pack_start(button("Edit", lambda p=p: self.add(p)), False, False, 0)
            self.cards.add(row)
        self.cards.show_all()

    def activate(self):
        if hasattr(self._screen, "kdj_motion"):
            self._screen.kdj_motion.disarm()
        self.refresh()

    def disconnected_callback(self):
        pass

    def find(self):
        self._screen.show_panel("kdj_discovery")

    def add(self, item=None):
        self._screen.kdj_edit = item
        self._screen.panels_reinit.append("kdj_connection")
        self._screen.show_panel("kdj_connection")


class Discovery(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Discover printers")
        self.content.get_style_context().add_class("kdj")
        self.content.set_spacing(12)
        self.content.set_border_width(16)
        self.status = label("Looking for Moonraker printers…", "kdj-heading")
        self.content.add(self.status)
        self.content.add(label("If a printer does not advertise itself, scan this Pi’s local network or add its address manually.", "kdj-muted"))
        bar = Gtk.Box(spacing=10, homogeneous=True)
        self.scan = button("Scan local network", lambda: self.search(True))
        bar.add(self.scan)
        bar.add(button("Add manually", self.manual))
        self.content.add(bar)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll.add(self.results)
        self.content.add(scroll)
        self.running = False
        self.search(False)

    def manual(self):
        self._screen.kdj_edit = None
        self._screen.panels_reinit.append("kdj_connection")
        self._screen.show_panel("kdj_connection")

    def search(self, scan):
        if self.running:
            return
        self.running = True
        self.scan.set_sensitive(False)
        self.status.set_text("Scanning local network…" if scan else "Listening for printers…")

        def worker():
            try:
                rows, error = discover(scan), None
            except Exception:
                rows, error = [], "Discovery unavailable. Check your network, or enter a printer address."
            GLib.idle_add(self.finish, rows, error)
        threading.Thread(target=worker, daemon=True).start()

    def finish(self, rows, error):
        self.running = False
        self.scan.set_sensitive(True)
        self.status.set_text(error or f"Found {len(rows)} connection(s)")
        clear(self.results)
        for url, name in rows:
            self.results.add(button(f"{name}\n{url}", lambda u=url: self.choose(u), "kdj-card"))
        self.results.show_all()
        return False

    def choose(self, url):
        self._screen.kdj_edit = {"name": "", "url": url, "api_key": "", "remote": False}
        self._screen.panels_reinit.append("kdj_connection")
        self._screen.show_panel("kdj_connection")


class Connection(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Printer connection")
        self.original = getattr(screen, "kdj_edit", None) or {}
        self.content.get_style_context().add_class("kdj")
        scroll = Gtk.ScrolledWindow(vexpand=True)
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        scroll.add(form)
        self.content.add(scroll)
        form.add(label("Make the connection", "kdj-heading"))
        self.fields = {}
        for key, title, hint in (("name", "Printer name", "Voron 2.4"),
                                 ("url", "Moonraker / OctoEverywhere app URL", "http://voron24.local:7125"),
                                 ("api_key", "Moonraker API key (if needed)", "Optional")):
            form.add(label(title))
            field = Gtk.Entry(text=self.original.get(key, ""), placeholder_text=hint)
            field.set_visibility(key not in ("api_key", "url"))
            field.set_size_request(-1, 52)
            field.connect("button-press-event", self.keyboard)
            form.add(field)
            self.fields[key] = field
        self.remote = Gtk.CheckButton(label="Remote connection · discrete joystick steps")
        self.remote.set_active(self.original.get("remote", False))
        form.add(self.remote)
        reveal = Gtk.CheckButton(label="Show connection URL")
        reveal.connect("toggled", lambda w: self.fields["url"].set_visibility(w.get_active()))
        form.add(reveal)
        form.add(label("OctoEverywhere: App Setup → Connect Another App or Slicer. Paste its secure custom connection URL here; a normal browser login link will not work. Custom connections require supporter access.", "kdj-muted"))
        self.result = label("Test the connection before saving.")
        self.content.pack_start(self.result, False, False, 6)
        row = Gtk.Box(spacing=10, homogeneous=True)
        self.test_button = button("Test connection", self.test)
        row.add(self.test_button)
        self.save_button = button("Save & reload", self.save, "kdj-accent")
        row.add(self.save_button)
        self.content.pack_start(row, False, False, 6)
        if self.original.get("name"):
            form.add(button("Remove connection", self.remove))

    def keyboard(self, widget, event):
        self._screen.show_keyboard(widget)
        return False

    def value(self):
        return profile(*(self.fields[k].get_text() for k in ("name", "url", "api_key")), self.remote.get_active())

    def test(self):
        try:
            value = self.value()
        except ValueError as exc:
            self.result.set_text(str(exc))
            return
        self.test_button.set_sensitive(False)
        self.result.set_text("Connecting…")

        def worker():
            client = Client(value)
            try:
                text = client.test()
            except Exception as exc:
                text = str(exc)
            finally:
                client.close()
            GLib.idle_add(self.test_result, text)
        threading.Thread(target=worker, daemon=True).start()

    def test_result(self, text):
        self.result.set_text(text)
        self.test_button.set_sensitive(True)
        return False

    def save(self):
        try:
            p = self.value()
            store = self._screen.kdj_store
            if any(x["name"] == p["name"] and x["name"] != self.original.get("name") for x in store.printers):
                raise ValueError("A printer already uses that name. Choose another.")
            if self.original.get("name") and self.original["name"] != p["name"]:
                store.data["printers"] = [x for x in store.printers if x["name"] != self.original["name"]]
            store.put(p)
            self._screen.kdj_restart()
        except (ValueError, OSError) as exc:
            self.result.set_text(str(exc))

    def remove(self):
        def remove():
            store = self._screen.kdj_store
            store.data["printers"] = [p for p in store.printers if p["name"] != self.original["name"]]
            store.save()
            self._screen.kdj_restart()
        self._screen.kdj_confirm("Remove this saved connection?", remove)


class GamepadSetup(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Gamepad setup")
        self.settings = screen.kdj_store.data["gamepad"]
        self.content.get_style_context().add_class("kdj")
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        scroll.add(self.form)
        self.content.add(scroll)
        self.live = label("Connect your gamepad", "kdj-heading")
        self.form.add(self.live)
        self.form.add(label("Release the sticks, hold your enable button, then move a stick. Jogging works only on the Move screen. No motion is sent from this setup screen.", "kdj-muted"))
        self.feedback = label("Choose an action, then press Learn and press a gamepad button.")
        self.form.add(self.feedback)
        self.form.add(button("Use this connected gamepad", self.select_device))
        self.form.add(button("Allow any connected gamepad", self.any_device))
        enable_row = Gtk.Box(spacing=10, homogeneous=True)
        self.enable_label = label("")
        enable_row.add(self.enable_label)
        enable_row.add(button("Learn hold-to-jog button", lambda: self.learn("enable"), "kdj-accent"))
        self.form.add(enable_row)
        mapping = Gtk.Box(spacing=10, homogeneous=True)
        self.action = Gtk.ComboBoxText()
        for key, text in ACTIONS.items():
            self.action.append(key, text)
        self.action.set_active_id("next")
        mapping.add(self.action)
        mapping.add(button("Learn shortcut button", lambda: self.learn(self.action.get_active_id())))
        self.form.add(mapping)
        macro_row = Gtk.Box(spacing=10, homogeneous=True)
        self.macro = Gtk.Entry(placeholder_text="Custom macro name, e.g. LOAD_FILAMENT")
        self.macro.connect("button-press-event", lambda w, e: screen.show_keyboard(w))
        macro_row.add(self.macro)
        macro_row.add(button("Learn macro button", self.learn_macro))
        self.form.add(macro_row)
        self.bindings = label("")
        self.form.add(self.bindings)
        for axis in "xyz":
            row = Gtk.Box(spacing=10, homogeneous=True)
            row.add(label(axis.upper() + " axis number"))
            number = Gtk.SpinButton.new_with_range(-1, 15, 1)
            number.set_value(self.settings["axes"][axis])
            number.connect("value-changed", self.change_axis, axis)
            row.add(number)
            invert = Gtk.CheckButton(label="Invert direction")
            invert.set_active(self.settings["invert"][axis])
            invert.connect("toggled", self.invert, axis)
            row.add(invert)
            self.form.add(row)
        self.form.add(label("Use the live values below to identify stick axes. −1 disables an axis. Choose centered sticks, not one-sided analog triggers.", "kdj-muted"))
        self.axes_label = label("")
        self.form.add(self.axes_label)
        row = Gtk.Box(spacing=10, homogeneous=True)
        row.add(label("Stick deadzone (%)"))
        deadzone = Gtk.SpinButton.new_with_range(10, 60, 1)
        deadzone.set_value(100 * self.settings["deadzone"])
        deadzone.connect("value-changed", self.deadzone)
        row.add(deadzone)
        self.form.add(row)
        self.refresh()
        self.timer = None

    def persist(self):
        self._screen.kdj_store.save()
        self._screen.kdj_motion.disarm()
        self.refresh()

    def refresh(self):
        self.enable_label.set_text(f'Hold-to-jog: {self.settings["enable_button"] if self.settings["enable_button"] is not None else "not assigned"}')
        self.bindings.set_text("\n".join(f"Button {key} → {ACTIONS.get(action, action)}" for key, action in sorted(self.settings["buttons"].items())) or "No shortcuts assigned yet.")

    def learn(self, action):
        pad = self._screen.kdj_pad
        if not pad or not pad.device:
            self.feedback.set_text("Connect a gamepad first.")
            return
        self.feedback.set_text("Press the gamepad button now…")

        def learned(index):
            if action == "enable":
                self.settings["enable_button"] = index
                self.settings["buttons"].pop(str(index), None)
            elif index == self.settings["enable_button"]:
                self.feedback.set_text("That is your hold-to-jog button. Choose a different button.")
                return
            elif action == "none":
                self.settings["buttons"].pop(str(index), None)
            else:
                self.settings["buttons"][str(index)] = action
            self.feedback.set_text(f"Saved button {index}.")
            self.persist()
        pad.learn = learned

    def select_device(self):
        pad = self._screen.kdj_pad
        if pad and pad.device:
            self.settings["guid"] = pad.device.get_guid()
            self.feedback.set_text("Saved this gamepad model. Other models will be ignored.")
            self.persist()

    def any_device(self):
        self.settings["guid"] = ""
        self.persist()
        self.feedback.set_text("The first connected gamepad can be used. Recheck its mapping before jogging.")

    def learn_macro(self):
        name = self.macro.get_text().strip().upper()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            self.feedback.set_text("Enter a macro name only, without spaces or parameters.")
            return
        self._screen.remove_keyboard()
        self.learn("macro:" + name)

    def change_axis(self, widget, axis):
        self.settings["axes"][axis] = widget.get_value_as_int()
        self.persist()

    def invert(self, widget, axis):
        self.settings["invert"][axis] = widget.get_active()
        self.persist()

    def deadzone(self, widget):
        self.settings["deadzone"] = widget.get_value() / 100
        self.persist()

    def activate(self):
        self._screen.kdj_motion.disarm()
        self.timer = GLib.timeout_add(200, self.update)

    def deactivate(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if self._screen.kdj_pad:
            self._screen.kdj_pad.learn = None

    def update(self):
        pad = self._screen.kdj_pad
        if pad:
            self.live.set_text(pad.name)
            self.axes_label.set_text("  ".join(f"Axis {i}: {value:+.2f}" for i, value in enumerate(pad.raw_axes)))
        return True

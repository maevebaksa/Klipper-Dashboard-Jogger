"""Touch-friendly dashboard and setup panels hosted inside KlipperScreen."""
import threading
import re
from gi.repository import Gtk, GLib
from ks_includes.screen_panel import ScreenPanel
from .config import profile
from .network import Client, discover
from .gamepad import ACTIONS
from .octoeverywhere import OctoEverywhereError, parse_completion, portal_url


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


def open_connection(screen, item=None):
    """Open the connection editor with fresh state without tripping panel reload."""
    screen.kdj_edit = item
    # panels_reinit is only valid for a panel that has already been constructed.
    # Adding a not-yet-loaded panel here makes upstream attach_panel() reload the
    # panel stack immediately, which sends the user back to the dashboard.
    if "kdj_connection" in screen.panels and "kdj_connection" not in screen.panels_reinit:
        screen.panels_reinit.append("kdj_connection")
    screen.show_panel("kdj_connection")


class Dashboard(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Your printers")
        self.content.get_style_context().add_class("kdj")
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=18)
        self.content.add(self.root)
        self.root.pack_start(label("KlipperController", "kdj-heading"), False, False, 0)
        self.root.pack_start(label("Select a printer to open KlipperScreen controls. Ctrl + Tab switches printers · F1 returns here", "kdj-muted"), False, False, 0)
        bar = Gtk.Box(spacing=10, homogeneous=True)
        for text, callback in (("Discover printers", self.find), ("Add connection", self.add),
                               ("Gamepad setup", lambda: screen.show_panel("kdj_gamepad"))):
            bar.add(button(text, callback, "kdj-accent"))
        self.root.pack_start(bar, False, False, 0)
        self.status = label("Moonraker connections", "kdj-muted")
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
            self.cards.add(label("No printers configured. Discover a printer or add its Moonraker address.", "kdj-empty"))
        for p in store.printers:
            row = Gtk.Box(spacing=10)
            # Never display remote credential-bearing URLs on the dashboard.
            if p.get("octoeverywhere"):
                subtitle = "LOCAL + OCTOEVERYWHERE"
            elif p["remote"]:
                subtitle = "REMOTE"
            else:
                subtitle = "LOCAL NETWORK"
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
        open_connection(self._screen, item)


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
        open_connection(self._screen)

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
        open_connection(
            self._screen,
            {"name": "", "url": url, "api_key": "", "remote": False},
        )


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
        self.remote = Gtk.CheckButton(label="Primary URL is remote")
        self.remote.set_active(self.original.get("remote", False))
        form.add(self.remote)
        reveal = Gtk.CheckButton(label="Show connection URL")
        reveal.connect("toggled", lambda w: self.fields["url"].set_visibility(w.get_active()))
        form.add(reveal)

        form.add(label("OctoEverywhere", "kdj-section"))
        self.oe_data = self.original.get("octoeverywhere")
        self.oe_printer_id = ""
        oe_settings = screen.kdj_store.data.setdefault("octoeverywhere", {"app_id": ""})

        form.add(label(
            "KlipperController can keep this local Moonraker address and use an "
            "OctoEverywhere App Connection when the LAN address is unavailable.",
            "kdj-muted",
        ))
        form.add(label("OctoEverywhere App ID"))
        self.oe_app_id = Gtk.Entry(
            text=oe_settings.get("app_id", ""),
            placeholder_text="App ID assigned by OctoEverywhere",
        )
        self.oe_app_id.set_size_request(-1, 52)
        self.oe_app_id.connect("button-press-event", self.keyboard)
        form.add(self.oe_app_id)

        self.oe_status = label(
            "Remote access linked." if self.oe_data else
            "Test the local connection first; OctoEverywhere detection is automatic.",
            "kdj-muted",
        )
        form.add(self.oe_status)
        oe_row = Gtk.Box(spacing=10, homogeneous=True)
        self.oe_button = button("Set up remote access", self.setup_octoeverywhere)
        oe_row.add(self.oe_button)
        self.oe_remove = button("Remove remote access", self.remove_octoeverywhere)
        self.oe_remove.set_sensitive(bool(self.oe_data))
        oe_row.add(self.oe_remove)
        form.add(oe_row)

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
            printer_id = ""
            try:
                text = client.test()
                if not value.get("remote"):
                    try:
                        printer_id = client.octoeverywhere_printer_id()
                    except Exception:
                        printer_id = ""
            except Exception as exc:
                text = str(exc)
            finally:
                client.close()
            GLib.idle_add(self.test_result, text, printer_id)
        threading.Thread(target=worker, daemon=True).start()

    def test_result(self, text, printer_id=""):
        self.result.set_text(text)
        self.oe_printer_id = printer_id
        if printer_id:
            if self.oe_data:
                self.oe_status.set_text("OctoEverywhere detected locally · remote access linked.")
            else:
                self.oe_status.set_text("OctoEverywhere detected locally · ready to set up remote access.")
        elif not self.oe_data:
            self.oe_status.set_text("OctoEverywhere printer ID was not found on this Moonraker connection.")
        self.test_button.set_sensitive(True)
        return False

    def setup_octoeverywhere(self):
        try:
            value = self.value()
        except ValueError as exc:
            self.oe_status.set_text(str(exc))
            return

        app_id = self.oe_app_id.get_text().strip()
        if not app_id:
            self.oe_status.set_text("Enter the App ID assigned to KlipperController by OctoEverywhere.")
            return
        if value.get("remote"):
            self.oe_status.set_text("Use a local Moonraker URL as the primary connection before adding dual access.")
            return

        self.oe_button.set_sensitive(False)
        self.oe_status.set_text("Checking the local printer for OctoEverywhere…")

        def worker():
            printer_id = self.oe_printer_id
            client = Client(value)
            try:
                client.test()
                if not printer_id:
                    printer_id = client.octoeverywhere_printer_id()
                url = portal_url(app_id, printer_id)
                error = None
            except Exception as exc:
                url, error = None, str(exc)
            finally:
                client.close()
            GLib.idle_add(self.open_octoeverywhere, value, app_id, printer_id, url, error)

        threading.Thread(target=worker, daemon=True).start()

    def open_octoeverywhere(self, value, app_id, printer_id, url, error):
        self.oe_button.set_sensitive(True)
        if error:
            self.oe_status.set_text(error)
            return False
        self._screen.kdj_store.data.setdefault("octoeverywhere", {})["app_id"] = app_id
        self._screen.kdj_store.save()
        self._screen.kdj_oe_pending = {
            "profile": value,
            "app_id": app_id,
            "printer_id": printer_id,
            "portal_url": url,
        }
        self._screen.show_panel("kdj_octoeverywhere")
        return False

    def remove_octoeverywhere(self):
        self.oe_data = None
        self.oe_remove.set_sensitive(False)
        self.oe_status.set_text("Remote access will be removed when this connection is saved.")


    def save(self):
        try:
            p = self.value()
            store = self._screen.kdj_store
            if any(x["name"] == p["name"] and x["name"] != self.original.get("name") for x in store.printers):
                raise ValueError("A printer already uses that name. Choose another.")
            if self.original.get("name") and self.original["name"] != p["name"]:
                store.data["printers"] = [x for x in store.printers if x["name"] != self.original["name"]]
            if self.oe_data:
                p["octoeverywhere"] = self.oe_data
            store.data.setdefault("octoeverywhere", {})["app_id"] = self.oe_app_id.get_text().strip()
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


class OctoEverywhereSetup(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "OctoEverywhere")
        self.content.get_style_context().add_class("kdj")
        pending = getattr(screen, "kdj_oe_pending", None) or {}
        self.pending = pending

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        self.content.add(root)
        self.status = label("Authorize KlipperController in OctoEverywhere.", "kdj-muted")
        root.pack_start(self.status, False, False, 0)

        try:
            import gi
            gi.require_version("WebKit2", "4.1")
            from gi.repository import WebKit2
        except (ImportError, ValueError):
            self.status.set_text(
                "The embedded OctoEverywhere browser is unavailable. Run the installer again "
                "to install WebKitGTK."
            )
            root.pack_start(label(pending.get("portal_url", ""), "kdj-muted"), False, False, 0)
            return

        self.web = WebKit2.WebView()
        self.web.set_hexpand(True)
        self.web.set_vexpand(True)
        self.web.connect("load-changed", self.load_changed)
        root.pack_start(self.web, True, True, 0)
        if pending.get("portal_url"):
            self.web.load_uri(pending["portal_url"])
        else:
            self.status.set_text("No OctoEverywhere setup request is active.")

    def load_changed(self, web, event):
        uri = web.get_uri() or ""
        parsed = None
        if "/appportal/v1/complete" in uri and "success=" in uri:
            try:
                parsed = parse_completion(uri)
            except OctoEverywhereError as exc:
                self.status.set_text(str(exc))
                return
        if not parsed:
            return

        web.stop_loading()
        profile_data = dict(self.pending.get("profile") or {})
        profile_data["octoeverywhere"] = parsed
        self._screen.kdj_edit = profile_data
        self._screen.kdj_store.data.setdefault("octoeverywhere", {})["app_id"] = self.pending.get("app_id", "")
        self._screen.kdj_store.save()
        self._screen.kdj_oe_pending = None
        if "kdj_connection" in self._screen.panels and "kdj_connection" not in self._screen.panels_reinit:
            self._screen.panels_reinit.append("kdj_connection")
        self._screen.show_panel("kdj_connection")


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
        self.device_status = label("", "kdj-muted")
        self.form.add(self.device_status)
        self.form.add(label(
            "Release the sticks, hold your enable button, then move a stick or a mapped Jog X/Y/Z button. "
            "Jogging works only on the Move screen. Direction buttons still require the hold-to-jog button.",
            "kdj-muted",
        ))
        self.feedback = label("Choose an action, then press Learn and press a gamepad button.")
        self.form.add(self.feedback)

        device_row = Gtk.Box(spacing=10, homogeneous=True)
        device_row.add(button("Use this connected gamepad", self.select_device))
        device_row.add(button("Allow any connected gamepad", self.any_device))
        self.form.add(device_row)

        self.form.add(label("Current button mappings", "kdj-section"))
        self.mapping_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.form.add(self.mapping_box)

        enable_row = Gtk.Box(spacing=10, homogeneous=True)
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
        clear(self.mapping_box)

        selected = self.settings.get("guid", "")
        if selected:
            self.device_status.set_text("Controller lock: saved gamepad model")
        else:
            self.device_status.set_text("Controller lock: any connected gamepad")

        enable = self.settings.get("enable_button")
        if enable is None:
            self.mapping_box.add(label("Hold-to-jog — not assigned", "kdj-mapping-empty"))
        else:
            row = Gtk.Box(spacing=10)
            row.get_style_context().add_class("kdj-mapping-row")
            row.pack_start(label(f"Button {enable}", "kdj-mapping-button"), False, False, 0)
            row.pack_start(label("Hold-to-jog", "kdj-mapping-action"), True, True, 0)
            self.mapping_box.add(row)

        for key, action in sorted(
            self.settings["buttons"].items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0]),
        ):
            row = Gtk.Box(spacing=10)
            row.get_style_context().add_class("kdj-mapping-row")
            row.pack_start(label(f"Button {key}", "kdj-mapping-button"), False, False, 0)
            action_text = ACTIONS.get(action, action[6:] if action.startswith("macro:") else action)
            if action.startswith("macro:"):
                action_text = f"Macro · {action[6:]}"
            row.pack_start(label(action_text, "kdj-mapping-action"), True, True, 0)
            self.mapping_box.add(row)

        if enable is None and not self.settings["buttons"]:
            self.mapping_box.add(label("No buttons mapped yet.", "kdj-mapping-empty"))

        self.mapping_box.show_all()

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

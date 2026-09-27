"""Touch-friendly dashboard and setup panels hosted inside KlipperScreen."""
import concurrent.futures
import sys
import threading
import re
import time
from gi.repository import Gtk, GLib
from ks_includes.screen_panel import ScreenPanel
from .config import profile
from .network import (
    Client, ConnectionError, discover, lan_fallback_url, prefer_hostname_url,
    select_endpoint, verify_remote,
)
from .gamepad import ACTIONS
from .handoff import Handoff
from .privacy import redact_logs
from . import updater
from .octoeverywhere import (
    OctoEverywhereError, describe, parse_completion, parse_shared_connection,
    portal_url,
)

# Dashboard card refresh. LAN checks are one small request per printer; remote
# checks go through OctoEverywhere, so they run less often to respect its limits.
DASHBOARD_REFRESH_S = 15
REMOTE_STATUS_REFRESH_S = 60

STATE_TEXT = {
    "standby": "Ready", "ready": "Ready", "printing": "Printing", "paused": "Paused",
    "complete": "Finished", "cancelled": "Cancelled", "error": "Error",
    "shutdown": "Klipper shut down", "startup": "Klipper starting",
}
ROUTE_LABEL = {"local": "local network", "octoeverywhere": "OctoEverywhere", "remote": "remote"}


def label(text, css=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def button(text, callback, css=None):
    widget = Gtk.Button(label=text)
    widget.set_size_request(-1, 48)
    widget.connect("clicked", lambda _w: callback())
    if css:
        widget.get_style_context().add_class(css)
    return widget


def clear(box):
    for child in box.get_children():
        box.remove(child)


def scroller():
    scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    try:
        scroll.set_overlay_scrolling(False)
    except AttributeError:
        pass
    return scroll


def qr_code(text, size=230):
    """A QR code drawn with cairo, or None when segno is not installed."""
    try:
        import segno
    except ImportError:
        return None
    rows = [list(row) for row in segno.make(text, error="m").matrix_iter(scale=1, border=4)]
    area = Gtk.DrawingArea()
    area.set_size_request(size, size)

    def draw(widget, cr):
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        cell = max(1, min(width, height) // len(rows))
        side = cell * len(rows)
        x0, y0 = (width - side) // 2, (height - side) // 2
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(x0, y0, side, side)
        cr.fill()
        cr.set_source_rgb(0, 0, 0)
        for y, row in enumerate(rows):
            for x, dark in enumerate(row):
                if dark:
                    cr.rectangle(x0 + x * cell, y0 + y * cell, cell, cell)
        cr.fill()
        return False

    area.connect("draw", draw)
    return area


def open_connection(screen, item=None):
    """Open the connection editor with fresh state without tripping panel reload."""
    screen.kdj_edit = item
    # panels_reinit is only valid for a panel that has already been constructed.
    # Adding a not-yet-loaded panel here makes upstream attach_panel() reload the
    # panel stack immediately, which sends the user back to the dashboard.
    if "kdj_connection" in screen.panels and "kdj_connection" not in screen.panels_reinit:
        screen.panels_reinit.append("kdj_connection")
    screen.show_panel("kdj_connection")


def status_text(summary, source):
    state = STATE_TEXT.get(summary["state"], summary["state"].capitalize())
    if summary["state"] in ("printing", "paused") and summary.get("progress") is not None:
        state += f" {round(summary['progress'] * 100)}%"
    return f"{state} · {ROUTE_LABEL.get(source, source)}"


class Dashboard(ScreenPanel):
    def __init__(self, screen, title=None):
        # Upstream can re-run __init__ on this same object; never leak a timer.
        if getattr(self, "timer", None) is not None:
            GLib.source_remove(self.timer)
        super().__init__(screen, title or "Your printers")
        self.content.get_style_context().add_class("kdj")
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.content.add(self.root)
        self.root.pack_start(label("KlipperController", "kdj-heading"), False, False, 0)
        self.root.pack_start(label("Choose a printer. F1 returns here · Ctrl + Tab switches printers.", "kdj-muted"), False, False, 0)
        bar = Gtk.Box(spacing=6, homogeneous=True)
        for text, callback in (
            ("Discover", self.find),
            ("Add", self.add),
            ("Network", lambda: screen.show_panel("network")),
            ("Gamepad", lambda: screen.show_panel("kdj_gamepad")),
            ("Update", lambda: screen.show_panel("kdj_update")),
        ):
            bar.add(button(text, callback, "kdj-accent"))
        self.root.pack_start(bar, False, False, 0)
        scroll = scroller()
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll.add(self.cards)
        self.root.pack_start(scroll, True, True, 0)
        self.card_buttons = {}
        self.statuses = getattr(self, "statuses", {})
        self.next_check = getattr(self, "next_check", {})
        self.timer = None
        self.polling = False
        self.generation = getattr(self, "generation", 0) + 1
        self.refresh()

    @staticmethod
    def access_text(p):
        if p.get("octoeverywhere"):
            return "Local, OctoEverywhere backup"
        return "Remote URL" if p["remote"] else "Local network"

    def refresh(self):
        clear(self.cards)
        self.card_buttons = {}
        store = self._screen.kdj_store
        if not store.printers:
            self.cards.add(label("No printers configured. Discover a printer or add its Moonraker address.", "kdj-empty"))
        for p in store.printers:
            row = Gtk.Box(spacing=10)
            card = button("", lambda p=p: self._screen.connect_printer(p["name"]), "kdj-card")
            self.card_buttons[p["name"]] = card
            self.set_card(p)
            row.pack_start(card, True, True, 0)
            row.pack_start(button("Edit", lambda p=p: self.add(p)), False, False, 0)
            self.cards.add(row)
        self.cards.show_all()

    def set_card(self, p):
        card = self.card_buttons.get(p["name"])
        if card is None:
            return
        # Never display credential-bearing remote URLs on the dashboard.
        line = self.statuses.get(p["name"]) or f"Checking… · {self.access_text(p)}"
        card.set_label(f"{p['name']}\n{line}")

    def activate(self):
        if hasattr(self._screen, "kdj_motion"):
            self._screen.kdj_motion.disarm()
        self.refresh()
        self.poll()
        if self.timer is None:
            self.timer = GLib.timeout_add_seconds(DASHBOARD_REFRESH_S, self.poll)

    def deactivate(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        self.generation += 1  # drop results that arrive after leaving

    def poll(self):
        if self.polling:
            return True
        now = time.monotonic()
        due = [dict(p) for p in self._screen.kdj_store.printers
               if self.next_check.get(p["name"], 0) <= now]
        if not due:
            return True
        self.polling = True
        generation = self.generation

        def check(p):
            try:
                selected = select_endpoint(p)
                client = Client(p, selected, timeout=(3, 8) if selected["remote"] else (1.5, 3))
                try:
                    text = status_text(client.summary(), selected["source"])
                finally:
                    client.close()
            except ConnectionError as exc:
                message = str(exc)
                if "Authorization" in message:
                    text = "Needs its API key or a new remote link"
                elif "OctoEverywhere" in message:
                    text = message
                else:
                    text = "Offline"
                selected = {"remote": bool(p.get("octoeverywhere"))}
            except Exception:
                text, selected = "Offline", {"remote": False}
            return p["name"], text, selected.get("remote", False)

        def worker():
            results = []
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(check, due))
            finally:
                GLib.idle_add(self.apply_statuses, generation, results)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def apply_statuses(self, generation, results):
        self.polling = False
        now = time.monotonic()
        for name, text, remote in results:
            self.next_check[name] = now + (REMOTE_STATUS_REFRESH_S if remote else DASHBOARD_REFRESH_S) - 1
            self.statuses[name] = text
        if generation == self.generation:
            for p in self._screen.kdj_store.printers:
                self.set_card(p)
        return False

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
        self.content.set_spacing(8)
        self.content.set_border_width(10)
        self.status = label("Looking for Moonraker printers…", "kdj-heading")
        self.content.add(self.status)
        self.content.add(label("Names come from Mainsail, Fluidd or the printer's host name. If a printer does not appear, scan this Pi's local network or add its address manually.", "kdj-muted"))
        bar = Gtk.Box(spacing=6, homogeneous=True)
        self.scan = button("Scan local network", lambda: self.search(True))
        bar.add(self.scan)
        bar.add(button("Add manually", self.manual))
        self.content.add(bar)
        scroll = scroller()
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
        self.status.set_text(error or f"Found {len(rows)} printer(s)")
        clear(self.results)
        saved = {}
        for p in self._screen.kdj_store.printers:
            for url in (p["url"], p.get("lan_fallback_url")):
                if url:
                    saved[url] = p
        for url, name in rows:
            existing = saved.get(url)
            if existing:
                text = f"{existing['name']}\nAlready saved · {url}"
                callback = lambda p=existing: open_connection(self._screen, p)
            else:
                text = f"{name}\n{url}"
                callback = lambda u=url, n=name: self.choose(u, n)
            self.results.add(button(text, callback, "kdj-card"))
        self.results.show_all()
        return False

    def choose(self, url, name=""):
        open_connection(
            self._screen,
            {"name": name, "url": url, "api_key": "", "remote": False,
             "_new": True, "_autotest": True},
        )


class Connection(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Printer connection")
        self.original = getattr(screen, "kdj_edit", None) or {}
        # Discovery results carry a suggested name but are not saved profiles.
        self.editing_name = "" if self.original.get("_new") else self.original.get("name", "")
        self.content.get_style_context().add_class("kdj")
        self.scroll = scroller()
        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        self.scroll.add(self.form)
        self.content.pack_start(self.scroll, True, True, 0)
        form = self.form
        form.add(label("Make the connection", "kdj-heading"))
        self.fields = {}
        for key, title, hint in (("name", "Printer name", "Filled in from the printer"),
                                 ("url", "Local Moonraker URL", "http://voron24.local:7125"),
                                 ("api_key", "Moonraker API key (if needed)", "Optional")):
            form.add(label(title))
            field = Gtk.Entry(text=self.original.get(key, ""), placeholder_text=hint)
            field.set_visibility(key != "api_key")
            field.set_size_request(-1, 44)
            field.connect("button-press-event", self.keyboard)
            form.add(field)
            self.fields[key] = field

        # The printer's own name replaces the field until the user types one.
        self.name_touched = bool(self.editing_name)
        self.setting_name = False
        self.fields["name"].connect("changed", self.name_changed)
        self.detected_name = ""
        self.use_name = button("Use the printer's name", self.apply_detected_name)
        self.use_name.set_no_show_all(True)
        form.add(self.use_name)

        self.lan_fallback = self.original.get("lan_fallback_url", "")
        self.lan_fallback_for = self.original.get("url", "")

        form.add(label("Remote access (OctoEverywhere)", "kdj-section"))
        self.oe_data = self.original.get("octoeverywhere")
        self.oe_printer_id = ""
        oe_settings = screen.kdj_store.data.setdefault("octoeverywhere", {"app_id": ""})
        form.add(label(
            "Optional. KlipperController uses the local network when it can and switches "
            "to OctoEverywhere when it cannot, then back again.",
            "kdj-muted",
        ))
        self.oe_status = label(self.oe_status_text(), "kdj-muted")
        form.add(self.oe_status)
        oe_row = Gtk.Box(spacing=10, homogeneous=True)
        self.oe_button = button("Link using your phone", self.setup_octoeverywhere, "kdj-accent")
        oe_row.add(self.oe_button)
        self.oe_remove = button("Remove remote access", self.remove_octoeverywhere)
        self.oe_remove.set_sensitive(bool(self.oe_data))
        oe_row.add(self.oe_remove)
        form.add(oe_row)

        advanced = Gtk.Expander(label="Advanced")
        advanced_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6)
        advanced.add(advanced_box)
        advanced_box.add(label(
            "OctoEverywhere App ID: only if OctoEverywhere assigned one to KlipperController. "
            "With an App ID, linking uses the App Connection portal instead of a Shared Connection.",
            "kdj-muted",
        ))
        self.oe_app_id = Gtk.Entry(
            text=oe_settings.get("app_id", ""),
            placeholder_text="Leave empty to use a Shared Connection",
        )
        self.oe_app_id.set_size_request(-1, 44)
        self.oe_app_id.connect("button-press-event", self.keyboard)
        advanced_box.add(self.oe_app_id)
        self.remote = Gtk.CheckButton(label="Primary URL is itself remote (legacy)")
        self.remote.set_active(self.original.get("remote", False))
        advanced_box.add(self.remote)
        form.add(advanced)

        self.result = label("Test the connection before saving.")
        self.content.pack_start(self.result, False, False, 6)
        row = Gtk.Box(spacing=10, homogeneous=True)
        self.test_button = button("Test connection", self.test)
        row.add(self.test_button)
        self.save_button = button("Save & reload", self.save, "kdj-accent")
        row.add(self.save_button)
        self.content.pack_start(row, False, False, 6)
        if self.editing_name:
            form.add(button("Remove connection", self.remove))
        if self.original.get("_autotest"):
            GLib.idle_add(self.test)

    def oe_status_text(self):
        if self.oe_data:
            return f"Linked: OctoEverywhere {describe(self.oe_data)}."
        return "Not linked."

    def keyboard(self, widget, event):
        # Keep the keyboard inside this panel so the form's scroller remains
        # present and usable while typing.
        self._screen.show_keyboard(widget, box=self.content)
        GLib.idle_add(self.scroll_to, widget)
        return False

    def scroll_to(self, widget):
        try:
            _x, y = widget.translate_coordinates(self.form, 0, 0)
            adj = self.scroll.get_vadjustment()
            target = max(adj.get_lower(), min(y - 12, adj.get_upper() - adj.get_page_size()))
            adj.set_value(target)
        except (TypeError, AttributeError):
            pass
        return False

    def name_changed(self, _widget):
        if not self.setting_name:
            self.name_touched = True

    def set_name(self, text):
        self.setting_name = True
        try:
            self.fields["name"].set_text(text)
        finally:
            self.setting_name = False

    def apply_detected_name(self):
        if self.detected_name:
            self.set_name(self.detected_name)
        self.use_name.hide()

    def value(self, allow_auto_name=False):
        name = self.fields["name"].get_text().strip()
        if allow_auto_name and not name:
            name = "Moonraker"
        return profile(
            name,
            self.fields["url"].get_text(),
            self.fields["api_key"].get_text(),
            self.remote.get_active(),
        )

    def test(self):
        try:
            value = self.value(allow_auto_name=True)
        except ValueError as exc:
            self.result.set_text(str(exc))
            return False
        self.test_button.set_sensitive(False)
        self.result.set_text("Connecting…")

        def worker():
            selected = {"url": value["url"], "remote": value["remote"], "source": "local"}
            client = Client(value, selected)
            details = {"name": "", "hostname": "", "oe_printer_id": ""}
            stable_url, fallback = value["url"], ""
            try:
                info = client.server_info()
                text = "Connected" if info["klippy_connected"] else "Moonraker found · Klipper offline"
                details = client.details()
                if not value["remote"]:
                    stable_url = prefer_hostname_url(value, details["hostname"])
                    fallback = lan_fallback_url(stable_url)
            except Exception as exc:
                text = str(exc)
            finally:
                client.close()
            GLib.idle_add(self.test_result, text, details, stable_url, fallback)
        threading.Thread(target=worker, daemon=True).start()
        return False

    def test_result(self, text, details, stable_url, fallback):
        self.result.set_text(text)
        self.detected_name = details["name"]
        if self.detected_name and self.detected_name != self.fields["name"].get_text().strip():
            if self.name_touched:
                self.use_name.set_label(f"Use the printer's name: {self.detected_name}")
                self.use_name.show()
            else:
                self.set_name(self.detected_name)
        if stable_url and stable_url != self.fields["url"].get_text().strip():
            self.fields["url"].set_text(stable_url)
        self.lan_fallback, self.lan_fallback_for = fallback, stable_url

        self.oe_printer_id = details["oe_printer_id"]
        if self.oe_data:
            self.oe_status.set_text(self.oe_status_text())
        elif self.oe_printer_id:
            self.oe_status.set_text("OctoEverywhere is installed on this printer. Link it for remote access.")
        self.test_button.set_sensitive(True)
        return False

    def setup_octoeverywhere(self):
        try:
            value = self.value(allow_auto_name=True)
        except ValueError as exc:
            self.oe_status.set_text(str(exc))
            return
        if value.get("remote"):
            self.oe_status.set_text("Use a local Moonraker URL as the primary connection before adding remote access.")
            return
        if self.fields["url"].get_text().strip() == self.lan_fallback_for and self.lan_fallback:
            value["lan_fallback_url"] = self.lan_fallback
        app_id = self.oe_app_id.get_text().strip()
        store = self._screen.kdj_store
        store.data.setdefault("octoeverywhere", {})["app_id"] = app_id
        store.save()
        self._screen.kdj_oe_pending = {
            "profile": value,
            "app_id": app_id,
            "printer_id": self.oe_printer_id,
            "original_name": self.editing_name,
        }
        self._screen.show_panel("kdj_octoeverywhere")

    def linked(self, profile_data):
        """Called by RemoteLink after the new link was saved."""
        self.original = profile_data
        self.editing_name = profile_data["name"]
        self.name_touched = True
        self.oe_data = profile_data["octoeverywhere"]
        self.set_name(profile_data["name"])
        self.fields["url"].set_text(profile_data["url"])
        self.oe_status.set_text(self.oe_status_text() + " Saved.")
        self.oe_remove.set_sensitive(True)

    def remove_octoeverywhere(self):
        self.oe_data = None
        self.oe_remove.set_sensitive(False)
        self.oe_status.set_text("Remote access will be removed when this connection is saved.")

    def save(self):
        try:
            p = self.value()
            store = self._screen.kdj_store
            if any(x["name"] == p["name"] and x["name"] != self.editing_name for x in store.printers):
                raise ValueError("A printer already uses that name. Choose another.")
            if self.editing_name and self.editing_name != p["name"]:
                store.data["printers"] = [x for x in store.printers if x["name"] != self.editing_name]
            if self.oe_data:
                p["octoeverywhere"] = self.oe_data
            if self.lan_fallback and p["url"] == self.lan_fallback_for:
                p["lan_fallback_url"] = self.lan_fallback
            store.data.setdefault("octoeverywhere", {})["app_id"] = self.oe_app_id.get_text().strip()
            store.put(p)
            self._screen.kdj_restart()
        except (ValueError, OSError) as exc:
            self.result.set_text(str(exc))

    def remove(self):
        def remove():
            store = self._screen.kdj_store
            store.data["printers"] = [p for p in store.printers if p["name"] != self.editing_name]
            store.save()
            self._screen.kdj_restart()
        self._screen.kdj_confirm("Remove this saved connection?", remove)


class RemoteLink(ScreenPanel):
    """Show a QR code; the phone finishes OctoEverywhere setup and hands back the link."""

    def __init__(self, screen, title=None):
        if getattr(self, "handoff", None) is not None:  # upstream re-ran __init__
            self.handoff.stop()
        super().__init__(screen, title or "Link remote access")
        self.content.get_style_context().add_class("kdj")
        self.handoff = None
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.content.add(self.body)

    def activate(self):
        # Build per visit: the pending printer changes between visits, and
        # upstream may reuse this panel object without calling __init__ again.
        self.stop()
        clear(self.body)
        pending = getattr(self._screen, "kdj_oe_pending", None) or {}
        self.pending = pending
        self.status = label("", "kdj-muted")
        if not pending.get("profile"):
            self.body.add(label("No remote access setup is active.", "kdj-heading"))
            self.body.show_all()
            return
        name = pending["profile"]["name"]
        app_mode = bool(pending.get("app_id"))
        if app_mode:
            def make_portal(return_url):
                return portal_url(pending["app_id"], pending.get("printer_id", ""), return_url)
            self.handoff = Handoff("app", self.submit, name, portal_url=make_portal)
        else:
            self.handoff = Handoff("shared", self.submit, name)
        try:
            url = self.handoff.start()
        except OSError:
            self.handoff = None
            self.body.add(label("Could not open the setup page on this controller's network.", "kdj-heading"))
            self.body.show_all()
            return

        self.body.add(label(f"Scan with your phone to link {name}", "kdj-heading"))
        row = Gtk.Box(spacing=16)
        qr = qr_code(url)
        if qr is not None:
            row.pack_start(qr, False, False, 0)
        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if app_mode:
            steps.add(label("1. Scan the code. Your phone must be on the same network as this controller."))
            steps.add(label("2. Sign in to OctoEverywhere and approve KlipperController."))
            steps.add(label("3. The phone returns here automatically when you are done."))
        else:
            steps.add(label("1. Scan the code. Your phone must be on the same network as this controller."))
            steps.add(label("2. Tap Open OctoEverywhere Shared Connections and copy the link for this printer."))
            steps.add(label("3. Paste it into the page and tap Link remote access."))
        steps.add(label("Or type this address on the phone:", "kdj-muted"))
        address = label(url)
        address.set_selectable(True)
        steps.add(address)
        row.pack_start(steps, True, True, 0)
        self.body.add(row)
        self.status.set_text("Waiting for your phone… This page stays open for 15 minutes.")
        self.body.add(self.status)
        self.body.add(button("Cancel", self._screen._menu_go_back))
        self.body.show_all()

    def deactivate(self):
        self.stop()

    def stop(self):
        if self.handoff is not None:
            self.handoff.stop()
            self.handoff = None

    def submit(self, fields):
        """Runs on the handoff server thread: parse, verify, then save on GTK."""
        GLib.idle_add(self.set_status, "Checking the link…")
        pending = self.pending
        printer = dict(pending["profile"])
        try:
            if "completion_url" in fields:
                oe = parse_completion(fields["completion_url"])
            else:
                oe = parse_shared_connection(fields["url"], fields["username"], fields["password"])
            details = verify_remote(printer, oe)
        except (OctoEverywhereError, ConnectionError) as exc:
            GLib.idle_add(self.set_status, str(exc))
            return False, str(exc)
        if oe.get("last_local_ip") and not printer.get("lan_fallback_url"):
            printer["lan_fallback_url"] = lan_fallback_url(printer["url"], oe["last_local_ip"])
        printer["octoeverywhere"] = oe
        name = details.get("name") or printer["name"]
        GLib.idle_add(self.save_link, pending, printer)
        return True, f"Remote access linked for {name}. The controller has saved it."

    def set_status(self, text):
        if getattr(self, "status", None) is not None:
            self.status.set_text(text)
        return False

    def save_link(self, pending, printer):
        # Persist immediately: App Connection credentials are returned only once.
        store = self._screen.kdj_store
        original = pending.get("original_name", "")
        if original and original != printer["name"]:
            store.data["printers"] = [p for p in store.printers if p["name"] != original]
        store.put({k: v for k, v in printer.items() if not k.startswith("_")})
        redact_logs([printer])  # launch.py only covered the profiles it started with
        self._screen.kdj_oe_pending = None
        self._screen.kdj_edit = printer
        self.stop()
        connection = self._screen.panels.get("kdj_connection")
        if self._screen._cur_panels and self._screen._cur_panels[-1] == "kdj_octoeverywhere":
            if connection is not None:
                connection.linked(printer)
            self._screen._menu_go_back()
        self._screen.kdj_message(f"{printer['name']}: OctoEverywhere remote access linked.")
        return False


class UpdatePanel(ScreenPanel):
    """Check for and install KlipperController updates without SSH."""

    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Update KlipperController")
        self.content.get_style_context().add_class("kdj")
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.content.add(self.body)
        self.busy = False
        self.generation = 0

    def activate(self):
        self.generation += 1
        self.check()

    def deactivate(self):
        self.generation += 1  # results from a check still running are dropped

    def show(self, heading, lines=(), actions=()):
        clear(self.body)
        self.body.add(label(heading, "kdj-heading"))
        for text in lines:
            self.body.add(label(text, "kdj-muted"))
        row = Gtk.Box(spacing=10, homogeneous=True)
        for text, callback, css in actions:
            row.add(button(text, callback, css))
        self.body.add(row)
        self.body.show_all()
        return False

    def check(self):
        if self.busy:
            return
        self.busy = True
        generation = self.generation
        self.show("Checking for updates…")

        def worker():
            try:
                result, error = updater.check(self._screen.kdj_source), None
            except updater.UpdateError as exc:
                result, error = None, str(exc)
            GLib.idle_add(self.checked, generation, result, error)

        threading.Thread(target=worker, daemon=True).start()

    def checked(self, generation, result, error):
        self.busy = False
        if generation != self.generation:
            return False
        back = ("Back", self._screen._menu_go_back, None)
        again = ("Check again", self.check, None)
        if error:
            return self.show("Could not check for updates", [error], [again, back])
        if result["dirty"] or result["ahead"]:
            return self.show("Update over SSH", [
                "This copy of KlipperController has local changes, so it will not update itself.",
                "Run: cd ~/Klipper-Dashboard-Jogger && bash scripts/update.sh",
            ], [back])
        if not result["behind"]:
            return self.show("KlipperController is up to date", [], [again, back])
        lines = [f"{result['behind']} update(s) available:"]
        lines += ["• " + text for text in result["changes"]]
        if result["behind"] > len(result["changes"]):
            lines.append(f"…and {result['behind'] - len(result['changes'])} more.")
        if result["needs_installer"]:
            lines.append("This update also changes system setup. After it installs, run "
                         "scripts/update.sh over SSH to finish.")
        lines.append("Installing restarts this screen. Prints keep running; jogging stops.")
        return self.show("Update available", lines,
                         [("Install and restart", self.confirm_install, "kdj-accent"), back])

    def confirm_install(self):
        self._screen.kdj_confirm("Install the update and restart KlipperController?", self.install)

    def install(self):
        if self.busy:
            return
        self.busy = True
        generation = self.generation
        self.show("Installing update…", ["This can take several minutes if Python packages changed. "
                                         "Keep the controller powered on."])

        def worker():
            try:
                result, error = updater.apply(self._screen.kdj_source, self._screen.kdj_upstream,
                                              sys.executable), None
            except updater.UpdateError as exc:
                result, error = None, str(exc)
            GLib.idle_add(self.installed, generation, result, error)

        threading.Thread(target=worker, daemon=True).start()

    def installed(self, generation, result, error):
        self.busy = False
        back = ("Back", self._screen._menu_go_back, None)
        if error:
            # Shown even if the user left: the checkout may be half updated.
            self._screen.kdj_message("Update failed: " + error)
            if generation == self.generation:
                self.show("Update failed", [error, "Nothing was restarted."], [back])
            return False
        if result["needs_installer"]:
            self._screen.kdj_message(
                "Updated. Run scripts/update.sh over SSH to finish system setup.")
        self._screen.kdj_restart()
        return False


class GamepadSetup(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Gamepad setup")
        self.settings = screen.kdj_store.data["gamepad"]
        self.content.get_style_context().add_class("kdj")
        self.scroll = scroller()
        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=10)
        self.scroll.add(self.form)
        self.content.pack_start(self.scroll, True, True, 0)
        self.live = label("Connect your gamepad", "kdj-heading")
        self.form.add(self.live)
        self.device_status = label("", "kdj-muted")
        self.form.add(self.device_status)
        self.form.add(label(
            "Hold the enable button while using a stick or mapped Jog X/Y/Z button. Jogging is active only on Move.",
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
        self.macro.connect("button-press-event", self.keyboard)
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

    def keyboard(self, widget, event):
        self._screen.show_keyboard(widget, box=self.content)
        GLib.idle_add(self.scroll_to, widget)
        return False

    def scroll_to(self, widget):
        try:
            _x, y = widget.translate_coordinates(self.form, 0, 0)
            adj = self.scroll.get_vadjustment()
            target = max(adj.get_lower(), min(y - 12, adj.get_upper() - adj.get_page_size()))
            adj.set_value(target)
        except (TypeError, AttributeError):
            pass
        return False

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

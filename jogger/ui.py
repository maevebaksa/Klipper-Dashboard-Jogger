"""Touch-friendly dashboard and setup panels hosted inside KlipperScreen."""
import os
import signal
import sys
import threading
import re
import time
from gi.repository import Gtk, GLib, Pango
from ks_includes.screen_panel import ScreenPanel
from .config import profile
from .network import (
    Client, ConnectionError, discover, lan_fallback_url, prefer_hostname_url,
    verify_remote,
)
from .status import fleet_summary, group, headline, route_text
from . import layout
from .terminal import host_for, ssh_argv
from .gamepad import ACTIONS
from .handoff import Handoff
from .privacy import redact_logs
from . import updater
from .octoeverywhere import (
    OctoEverywhereError, describe, parse_completion, parse_shared_connection,
    portal_url,
)

# A full update runs apt, pip and the installer on a Pi; apt alone can take
# several minutes on a slow mirror. Poll the unit cheaply until it ends.
FULL_UPDATE_POLL_S = 3
FULL_UPDATE_TIMEOUT_S = 30 * 60


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


class PrinterTile:
    """One printer at a glance: name, state, progress, file and route."""

    def __init__(self, name, on_click, compact=False):
        self.name = name
        self.button = Gtk.Button()
        self.button.set_size_request(200 if compact else layout.MIN_TILE_W,
                                     118 if compact else layout.MAX_TILE_H)
        self.show_detail = True
        self.show_route = True
        self.button.connect("clicked", lambda _w: on_click(name))
        style = self.button.get_style_context()
        style.add_class("kdj-tile")
        # Ellipsized labels still ask for their full text width unless capped,
        # which would make the switcher popup wider than the screen.
        self.max_chars = 20 if compact else 28
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.button.add(box)
        top = Gtk.Box(spacing=6)
        self.title = self._line(name, "kdj-tile-name")
        top.pack_start(self.title, True, True, 0)
        self.badge = Gtk.Label(label="")
        self.badge.get_style_context().add_class("kdj-tile-badge")
        top.pack_end(self.badge, False, False, 0)
        box.pack_start(top, False, False, 0)
        self.state = self._line("", "kdj-tile-state")
        box.pack_start(self.state, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_no_show_all(True)
        box.pack_start(self.progress, False, False, 2)
        self.detail = self._line("", "kdj-tile-detail")
        self.detail.set_no_show_all(True)
        box.pack_start(self.detail, False, False, 0)
        self.route = self._line("", "kdj-tile-route")
        self.route.set_no_show_all(True)
        box.pack_end(self.route, False, False, 0)
        self.state_class = None

    def _line(self, text, css):
        widget = Gtk.Label(label=text, xalign=0)
        widget.set_ellipsize(Pango.EllipsizeMode.END)
        widget.set_max_width_chars(self.max_chars)
        widget.get_style_context().add_class(css)
        return widget

    def measure(self):
        """Minimum content heights: (name + state + progress, + route line, + file line).

        Measured with the progress bar and badge showing, the tallest a tile
        gets, so a printer that starts printing later cannot overflow the grid.
        """
        was = (self.progress.get_visible(), self.route.get_visible(),
               self.detail.get_visible(), self.badge.get_text())
        self.button.set_size_request(layout.MIN_TILE_W, -1)
        self.progress.show()
        self.badge.set_text("CURRENT")
        heights = []
        for route, detail in ((False, False), (True, False), (True, True)):
            self.route.set_visible(route)
            self.detail.set_visible(detail)
            heights.append(self.button.get_preferred_height()[0])
        self.progress.set_visible(was[0])
        self.route.set_visible(was[1])
        self.detail.set_visible(was[2])
        self.badge.set_text(was[3])
        return tuple(heights)

    def set_height(self, height, show_route, show_detail):
        """Fit the dashboard grid; short tiles drop the file line, then the route line."""
        self.button.set_size_request(layout.MIN_TILE_W, height)
        self.show_route = show_route
        self.show_detail = show_detail
        self.route.set_visible(show_route and bool(self.route.get_text()))
        self.detail.set_visible(show_detail and bool(self.detail.get_text()))

    def update(self, status, current=False, selected=False):
        self.state.set_text(headline(status))
        # Never show credential-bearing remote URLs; only the route name.
        detail = status.get("filename") or status.get("detail") or ""
        self.detail.set_text(detail)
        self.detail.set_visible(self.show_detail and bool(detail))
        route = route_text(status)
        self.route.set_text(route)
        self.route.set_visible(self.show_route and bool(route))
        self.badge.set_text("CURRENT" if current else "")
        if status.get("progress") is not None:
            self.progress.set_fraction(max(0.0, min(1.0, status["progress"])))
            self.progress.show()
        else:
            self.progress.hide()
        style = self.button.get_style_context()
        wanted = "kdj-state-" + group(status["state"])
        if wanted != self.state_class:
            if self.state_class:
                style.remove_class(self.state_class)
            style.add_class(wanted)
            self.state_class = wanted
        if selected:
            style.add_class("kdj-tile-selected")
        else:
            style.remove_class("kdj-tile-selected")


def current_printer(screen):
    state = screen.state
    return state.printer_name if state.connected and state.initialized else None


class Dashboard(ScreenPanel):
    """A quick glance at every printer. Setup lives behind Manage."""

    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Printers")
        self.content.get_style_context().add_class("kdj")
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.content.add(root)
        header = Gtk.Box(spacing=10)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading.pack_start(label("Printers", "kdj-heading"), False, False, 0)
        self.summary = label("", "kdj-muted")
        self.summary.set_line_wrap(False)
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        heading.pack_start(self.summary, False, False, 0)
        header.pack_start(heading, True, True, 0)
        manage = button("Manage", lambda: screen.show_panel("kdj_manage"))
        manage.set_size_request(140, 48)
        header.pack_end(manage, False, False, 0)
        ssh = button("SSH", lambda: screen.show_panel("kdj_ssh"))
        ssh.set_size_request(110, 48)
        header.pack_end(ssh, False, False, 0)
        root.pack_start(header, False, False, 0)
        scroll = scroller()
        self.flow = Gtk.FlowBox(homogeneous=True, selection_mode=Gtk.SelectionMode.NONE,
                                min_children_per_line=1, max_children_per_line=4,
                                row_spacing=layout.GAP, column_spacing=layout.GAP,
                                valign=Gtk.Align.START)
        scroll.add(self.flow)
        # Fit the grid to the space KlipperScreen actually leaves us, so nine
        # printers show without scrolling on any supported panel.
        self.area = (0, 0)
        self.layout_key = None
        self.slack = 0
        scroll.connect("size-allocate", self.on_allocate)
        self.flow.connect("size-allocate", self.on_flow_allocate)
        root.pack_start(scroll, True, True, 0)
        hint = label("Tap a printer · Ctrl + Tab switches · Ctrl + R refreshes · F1 returns here",
                     "kdj-hint")
        hint.set_line_wrap(False)
        hint.set_ellipsize(Pango.EllipsizeMode.END)
        root.pack_start(hint, False, False, 0)
        self.tiles = {}
        self.rebuild()

    def on_allocate(self, _widget, allocation):
        area = (allocation.width, allocation.height)
        if area != self.area:
            self.area = area
            self.slack = 0
            # Resizing children inside size-allocate would re-enter layout.
            GLib.idle_add(self.apply_layout)

    def on_flow_allocate(self, _widget, allocation):
        # Safety net for anything the measurement missed (theme margins, a
        # font change): if the grid still overflows, lay out again with that
        # much less height. Bounded so it cannot loop.
        overflow = allocation.height - self.area[1]
        if 0 < overflow and self.slack < 200 and self.fits_without_scroll():
            self.slack += overflow
            self.layout_key = None
            GLib.idle_add(self.apply_layout)

    def fits_without_scroll(self):
        return self.layout_key is not None and self.layout_key[-1]

    def apply_layout(self):
        width, height = self.area
        if not self.tiles or width <= 1 or height <= 1:
            return False
        key = (len(self.tiles), width, height, self.slack)
        if self.layout_key is not None and key == self.layout_key[:-1]:
            return False
        # Every tile shares one structure, so one measurement covers them all.
        compact_h, route_h, full_h = next(iter(self.tiles.values())).measure()
        room = height - self.slack
        cols, tile_h = layout.grid(len(self.tiles), width, room,
                                   min_h=compact_h, max_h=max(layout.MAX_TILE_H, full_h))
        rows = -(-len(self.tiles) // cols)
        fits = rows * tile_h + (rows - 1) * layout.GAP <= room
        self.layout_key = key + (fits,)
        self.flow.set_min_children_per_line(cols)
        self.flow.set_max_children_per_line(cols)
        for tile in self.tiles.values():
            tile.set_height(tile_h, tile_h >= route_h, tile_h >= full_h)
        return False

    def refresh(self):
        """Ctrl + R: reload the printer list and re-check every printer now."""
        self.rebuild()
        self.summary.set_text("Refreshing…")
        self._screen.kdj_status.refresh_now()
        self._screen.kdj_status.poll()

    def rebuild(self):
        clear(self.flow)
        self.tiles = {}
        store = self._screen.kdj_store
        if not store.printers:
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            empty.add(label("No printers yet. Discover a printer or add its Moonraker address.", "kdj-empty"))
            empty.add(button("Add a printer", lambda: self._screen.show_panel("kdj_manage"), "kdj-accent"))
            self.flow.add(empty)
        for p in store.printers:
            tile = PrinterTile(p["name"], self._screen.connect_printer)
            self.tiles[p["name"]] = tile
            self.flow.add(tile.button)
        self.flow.show_all()
        self.layout_key = None
        self.slack = 0
        self.apply_layout()
        self.update()

    def update(self):
        monitor = self._screen.kdj_status
        current = current_printer(self._screen)
        statuses = []
        for name, tile in self.tiles.items():
            status = monitor.get(name)
            statuses.append(status)
            tile.update(status, current=name == current)
        self.summary.set_text(fleet_summary(statuses))

    def kdj_status_changed(self):
        self.update()

    def activate(self):
        if hasattr(self._screen, "kdj_motion"):
            self._screen.kdj_motion.disarm()
        self.rebuild()
        self._screen.kdj_status.poll()

    def disconnected_callback(self):
        pass


class Manage(ScreenPanel):
    """Setup: discovery, connections, network, gamepad and updates."""

    def __init__(self, screen, title=None):
        super().__init__(screen, title or "Manage")
        self.content.get_style_context().add_class("kdj")
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.content.add(root)
        root.pack_start(label("Manage", "kdj-heading"), False, False, 0)
        bar = Gtk.Box(spacing=6, homogeneous=True)
        for text, callback in (
            ("Discover", lambda: screen.show_panel("kdj_discovery")),
            ("Add", lambda: open_connection(screen)),
            ("Network", lambda: screen.show_panel("network")),
            ("Gamepad", lambda: screen.show_panel("kdj_gamepad")),
            ("Update", lambda: screen.show_panel("kdj_update")),
        ):
            bar.add(button(text, callback, "kdj-accent"))
        root.pack_start(bar, False, False, 0)
        root.pack_start(label("Saved printers", "kdj-section"), False, False, 0)
        scroll = scroller()
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll.add(self.rows)
        root.pack_start(scroll, True, True, 0)
        self.refresh()

    @staticmethod
    def access_text(p):
        if p.get("octoeverywhere"):
            return f"Local network, OctoEverywhere {describe(p['octoeverywhere'])} backup"
        return "Remote URL" if p["remote"] else "Local network"

    def refresh(self):
        clear(self.rows)
        store = self._screen.kdj_store
        if not store.printers:
            self.rows.add(label("No printers configured. Discover a printer or add its Moonraker address.", "kdj-empty"))
        for p in store.printers:
            row = Gtk.Box(spacing=10)
            row.get_style_context().add_class("kdj-mapping-row")
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.pack_start(label(p["name"], "kdj-mapping-button"), False, False, 0)
            text.pack_start(label(self.access_text(p), "kdj-muted"), False, False, 0)
            row.pack_start(text, True, True, 0)
            edit = button("Edit", lambda p=p: open_connection(self._screen, p))
            edit.set_size_request(110, 48)
            row.pack_end(edit, False, False, 0)
            self.rows.add(row)
        self.rows.show_all()

    def activate(self):
        self._screen.kdj_motion.disarm()
        self.refresh()


class Switcher:
    """Alt-Tab style printer picker shown over whatever panel is open.

    Keyboard: Ctrl + Tab opens it and steps; releasing Ctrl switches; Escape
    cancels. Gamepad: next/previous open it and step; the choice is made after
    a short pause. Touch: tap a printer.
    """

    def __init__(self, screen):
        self.screen = screen
        self.window = None
        self.names = []
        self.index = 0
        self.tiles = []
        self.hint = None

    @property
    def visible(self):
        return self.window is not None

    def selected(self):
        return self.names[self.index] if self.visible and self.names else None

    def open(self, names, index, hold):
        self.close()
        self.names = list(names)
        self.index = index % len(self.names)
        window = Gtk.Window(type=Gtk.WindowType.POPUP)
        window.set_transient_for(self.screen)
        window.set_accept_focus(False)
        # With no window manager, X focus can follow the pointer onto this
        # popup; hand any keys it receives to the main window's handlers.
        window.connect("key-press-event", self.screen._key_press_event)
        window.connect("key-release-event", self.screen.kdj_key_release)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        frame.get_style_context().add_class("kdj")
        frame.get_style_context().add_class("kdj-switcher")
        window.add(frame)
        frame.pack_start(label("Switch printer", "kdj-heading"), False, False, 0)
        screen_width, screen_height = self.screen.get_size()
        # A compact tile needs about 240 px including padding at KlipperScreen's
        # font sizes; never put more in a row than the screen can hold.
        per_line = min(len(self.names), 5 if len(self.names) > 6 else 3 if len(self.names) > 4 else 4,
                       max(1, (screen_width - 60) // 240))
        flow = Gtk.FlowBox(homogeneous=True, selection_mode=Gtk.SelectionMode.NONE,
                           min_children_per_line=per_line, max_children_per_line=per_line,
                           row_spacing=10, column_spacing=10)
        self.tiles = []
        for number, name in enumerate(self.names, 1):
            tile = PrinterTile(name, self.screen.kdj_switch_to, compact=True)
            if number <= 9:
                tile.title.set_text(f"{number}  {name}")  # press the number to jump there
            self.tiles.append(tile)
            flow.add(tile.button)
        frame.pack_start(flow, False, False, 0)
        self.hint = label("", "kdj-muted")
        frame.pack_start(self.hint, False, False, 0)
        self.set_hold(hold)
        self.window = window
        self.update()
        window.show_all()
        width, height = window.get_size()
        # KlipperScreen's screen-scaled font makes tiles ask for more width
        # than a row of five has; cap at the screen and let the text ellipsize.
        if width > screen_width - 40:
            window.resize(screen_width - 40, 1)
            width = screen_width - 40
            # The new height is known after GTK lays out again; re-center then.
            GLib.idle_add(self.center, window)
        x, y = self.screen.get_position()
        window.move(x + max(0, (screen_width - width) // 2), y + max(0, (screen_height - height) // 2))

    def center(self, window):
        if window is self.window:
            screen_width, screen_height = self.screen.get_size()
            width, height = window.get_size()
            x, y = self.screen.get_position()
            window.move(x + max(0, (screen_width - width) // 2), y + max(0, (screen_height - height) // 2))
        return False

    def set_hold(self, hold):
        if self.hint is not None:
            self.hint.set_text("Tab steps · release Ctrl to switch · or press a printer's number · Esc cancels"
                               if hold else
                               "Switching in a moment · press again to move on · tap a printer to pick it")

    def step(self, delta):
        if self.names:
            self.index = (self.index + delta) % len(self.names)
            self.update()

    def update(self):
        monitor = self.screen.kdj_status
        current = current_printer(self.screen)
        for i, tile in enumerate(self.tiles):
            tile.update(monitor.get(tile.name), current=tile.name == current, selected=i == self.index)

    def close(self):
        window, self.window = self.window, None
        self.tiles = []
        self.hint = None
        if window is not None:
            window.destroy()


class SshPanel(ScreenPanel):
    """An embedded terminal that starts ssh: host, then user name, then password."""

    def __init__(self, screen, title=None):
        if getattr(self, "pid", None):  # upstream re-ran __init__
            self.stop()
        super().__init__(screen, title or "SSH")
        self.content.get_style_context().add_class("kdj")
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        self.content.add(self.body)
        self.term = None
        self.pid = None

    def target_host(self):
        store = self._screen.kdj_store
        name = current_printer(self._screen) or self._screen.state.printer_name
        printer = next((p for p in store.printers if p["name"] == name), None)
        if printer is None and store.printers:
            printer = store.printers[0]
        return host_for(printer)

    def activate(self):
        self._screen.kdj_motion.disarm()
        self.stop()
        clear(self.body)
        try:
            import gi
            gi.require_version("Vte", "2.91")
            from gi.repository import Vte
        except (ImportError, ValueError):
            self.body.add(label("Terminal support is not installed yet.", "kdj-heading"))
            self.body.add(label("The installer adds it. Finish setup opens the Update screen, where Run installer "
                                "adds it (or run scripts/update.sh over SSH).", "kdj-muted"))
            row = Gtk.Box(spacing=10, homogeneous=True)
            row.add(button("Finish setup", lambda: self._screen.show_panel("kdj_update"), "kdj-accent"))
            row.add(button("Back", self._screen._menu_go_back))
            self.body.add(row)
            self.body.show_all()
            return
        bar = Gtk.Box(spacing=8)
        bar.pack_start(label("Keys go to the terminal · Ctrl + Tab and F1 still work", "kdj-muted"),
                       True, True, 0)
        restart = button("New session", self.start)
        restart.set_size_request(150, 44)
        bar.pack_end(restart, False, False, 0)
        close = button("Close", self._screen._menu_go_back)
        close.set_size_request(110, 44)
        bar.pack_end(close, False, False, 0)
        self.body.pack_start(bar, False, False, 0)

        term = Vte.Terminal()
        term.kdj_terminal = True  # the window's key handler passes keys through
        term.set_hexpand(True)
        term.set_vexpand(True)
        term.set_scrollback_lines(5000)
        term.set_font_scale(1.15)
        term.set_colors(self._rgba("#edf2f8"), self._rgba("#0b1017"), None)
        term.connect("child-exited", self.exited)
        self.body.pack_start(term, True, True, 0)
        self.term = term
        self.body.show_all()
        self.start()

    @staticmethod
    def _rgba(hex_color):
        from gi.repository import Gdk
        color = Gdk.RGBA()
        color.parse(hex_color)
        return color

    def start(self):
        if self.term is None:
            return
        self.stop()
        self.term.reset(True, True)
        from gi.repository import Vte
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), ssh_argv(self.target_host()),
            None, GLib.SpawnFlags.DEFAULT, None, None, -1, None, self.spawned, None,
        )
        self.term.grab_focus()

    def spawned(self, term, pid, error, *_args):
        if error is not None:
            term.feed(f"Could not start the terminal: {error.message}\r\n".encode())
            return
        self.pid = pid

    def exited(self, term, _status):
        self.pid = None
        term.feed(b"\r\n[Session ended. Tap New session to start another.]\r\n")

    def deactivate(self):
        # Leaving the panel ends the session rather than leaving ssh orphaned.
        self.stop()

    def stop(self):
        pid, self.pid = self.pid, None
        if pid:
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass


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

    def data_dir(self):
        return os.path.dirname(self._screen.kdj_upstream)

    def check(self):
        if self.busy:
            return
        self.busy = True
        generation = self.generation
        self.show("Checking for updates…")
        source, data_dir = self._screen.kdj_source, self.data_dir()

        def worker():
            try:
                result, error = updater.check(source), None
            except updater.UpdateError as exc:
                result, error = {"behind": 0, "ahead": 0, "dirty": False, "changes": [],
                                 "needs_installer": False}, str(exc)
            result["full"] = updater.can_sudo()
            result["setup"] = updater.setup_state(source, data_dir)
            result["running"] = result["full"] and updater.system_update_running()
            result["log"] = updater.read_update_log(data_dir)
            GLib.idle_add(self.checked, generation, result, error)

        threading.Thread(target=worker, daemon=True).start()

    def checked(self, generation, result, error):
        self.busy = False
        if generation != self.generation:
            return False
        self.full = result["full"]
        back = ("Back", self._screen._menu_go_back, None)
        again = ("Check again", self.check, None)
        if result["running"]:
            # Started earlier (this screen was left or the app restarted early).
            self.busy = True
            self.watch_started(generation, "An update or installer run is in progress…")
            return False

        local_changes = result["dirty"] or result["ahead"]
        can_update = result["behind"] and not local_changes and not error
        setup = result["setup"]
        lines, actions = [], []
        if error:
            heading = "Could not check for updates"
            lines.append(error)
        elif local_changes:
            heading = "Update over SSH"
            lines.append("This copy has local changes, so it will not update itself. "
                         "Run: cd ~/Klipper-Dashboard-Jogger && bash scripts/update.sh")
        elif result["behind"]:
            heading = "Update available"
            lines.append(f"{result['behind']} update(s):")
            lines += ["• " + text for text in result["changes"]]
            if result["behind"] > len(result["changes"]):
                lines.append(f"…and {result['behind'] - len(result['changes'])} more.")
        elif setup != "current":
            heading = "System setup needs to finish"
        else:
            heading = "KlipperController is up to date"

        if setup == "missing":
            lines.append("The installer has not completed since setup tracking was added, "
                         "or its last run failed.")
        elif setup == "stale":
            lines.append("The installer has changed since it last ran, so new system packages "
                         "(such as the SSH terminal) may be missing.")
        if setup != "current" and result["log"]:
            self.log_lines = result["log"]
        else:
            self.log_lines = []

        if self.full:
            lines.append("Update runs scripts/update.sh: git pull, then the installer. "
                         "Run installer runs scripts/install.sh alone, to finish or repair setup. "
                         "Both restart this screen; prints keep running.")
            if can_update:
                actions.append(("Update", lambda: self.confirm("update"), "kdj-accent"))
            actions.append(("Run installer", lambda: self.confirm("install"),
                            None if can_update else "kdj-accent"))
        else:
            if can_update:
                actions.append(("Update", lambda: self.confirm("update"), "kdj-accent"))
            if setup != "current" or result["needs_installer"]:
                lines.append("System setup needs sudo, which asks for a password on this Pi. "
                             "Run over SSH: cd ~/Klipper-Dashboard-Jogger && bash scripts/update.sh")
        actions += [again, back]
        return self.show(heading, lines, actions)

    def show(self, heading, lines=(), actions=()):
        clear(self.body)
        self.body.add(label(heading, "kdj-heading"))
        for text in lines:
            self.body.add(label(text, "kdj-muted"))
        row = Gtk.Box(spacing=10, homogeneous=True)
        for text, callback, css in actions:
            row.add(button(text, callback, css))
        self.body.add(row)
        log = getattr(self, "log_lines", [])
        if log:
            self.body.add(label("Last run ended with:", "kdj-section"))
            text = Gtk.Label(label="\n".join(log), xalign=0, selectable=True)
            text.set_line_wrap(True)
            text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            text.get_style_context().add_class("kdj-log")
            self.body.add(text)
        self.body.show_all()
        return False

    def confirm(self, kind):
        text = ("Update KlipperController and restart it?" if kind == "update" else
                "Run the installer and restart KlipperController?")
        self._screen.kdj_confirm(text, lambda: self.install(kind))

    def install(self, kind="update"):
        if self.busy:
            return
        if self._screen.kdj_motion.busy:
            self._screen.kdj_message("Wait for the current move to finish, then update.")
            return
        self._screen.kdj_motion.disarm()
        self.busy = True
        self.log_lines = []
        generation = self.generation
        if getattr(self, "full", False):
            return self.install_full(generation, installer_only=kind == "install")
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

    def install_full(self, generation, installer_only):
        what = ("scripts/install.sh" if installer_only else
                "scripts/update.sh: git pull, then the installer")
        self.show("Running the installer…" if installer_only else "Updating…", [
            f"Running {what}. This can take several minutes.",
            "The screen restarts by itself when it is done. Keep the controller powered on.",
        ])
        source, data_dir = self._screen.kdj_source, self.data_dir()

        def worker():
            try:
                updater.start_system_update(source, data_dir, installer_only=installer_only)
                error = None
            except updater.UpdateError as exc:
                error = str(exc)
            GLib.idle_add(self.full_started, generation, error)

        threading.Thread(target=worker, daemon=True).start()

    def full_started(self, generation, error):
        if error:
            return self.installed(generation, None, error)
        return self.watch_started(generation, None)

    def watch_started(self, generation, heading):
        if heading:
            self.show(heading, ["The screen restarts by itself when it is done."])
        # Success ends this process when the installer restarts the service.
        # If the unit stops and we are still running, the run failed.
        self.full_deadline = time.monotonic() + FULL_UPDATE_TIMEOUT_S
        GLib.timeout_add_seconds(FULL_UPDATE_POLL_S, self.watch_full, generation)
        return False

    def watch_full(self, generation):
        if not self.busy:
            return False
        if time.monotonic() > self.full_deadline:
            return self.full_stopped(generation, "It is still running after 30 minutes.")

        def worker():
            if not updater.system_update_running():
                GLib.idle_add(self.full_stopped, generation,
                              "It stopped before restarting the app.")
        threading.Thread(target=worker, daemon=True).start()
        return True

    def full_stopped(self, generation, reason):
        if not self.busy:
            return False  # already reported
        self.busy = False
        self._screen.kdj_message(f"Update did not finish. {reason}")
        if generation == self.generation:
            self.log_lines = updater.read_update_log(self.data_dir())
            log_path = os.path.join(self.data_dir(), updater.UPDATE_LOG)
            self.show("Update did not finish", [
                reason, f"Full output: {log_path} (or journalctl -u kdj-update -b)"],
                [("Run installer", lambda: self.confirm("install"), "kdj-accent"),
                 ("Back", self._screen._menu_go_back, None)])
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

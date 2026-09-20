"""Small adapter to the pinned upstream window; no upstream files are patched."""
import os
import sys
import types
import threading
from urllib.parse import urlsplit
import websocket
from gi.repository import Gdk, GLib, Gtk
from .motion import Motion
from .network import Client, select_endpoint
from .gamepad import Gamepad


def make_window(Base, store, source):
    import screen as upstream
    from ks_includes.KlippyWebsocket import KlippyWebsocket

    class BoundSocket(KlippyWebsocket):
        def __init__(self, callback, host, port, path="", ssl=None):
            # Pinned screen.py passes (host, port, path, ssl); transport also has
            # a legacy api_key positional parameter, unused by that transport.
            owner = callback["on_connect"].__self__
            self.owner = owner
            guarded = {key: self.guard(fn) for key, fn in callback.items()}
            super().__init__(guarded, host, port, "", path, ssl)
            self.callback_table = {}

        def guard(self, callback):
            def guarded(*args):
                if self.owner._ws is self:
                    return callback(*args)
                return False
            return guarded

        def connect(self):
            if self.connected:
                return False
            self.closing = False
            self.connecting = True
            self.ws_url = f"{self.ws_proto}://{self._url}/websocket"
            headers = ["User-Agent: KlipperScreen"]
            endpoint = getattr(self.owner, "kdj_endpoint", None) or {}
            if endpoint.get("authorization"):
                headers.append("Authorization: " + endpoint["authorization"])
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                header=headers,
                on_close=self.on_close,
                on_error=self.on_error,
                on_message=self.on_message,
                on_open=self.on_open,
            )
            self._wst = threading.Thread(target=self.ws.run_forever, daemon=True)
            try:
                self._wst.start()
            except Exception:
                return True
            return False

        def send_method(self, method, params=None, callback=None, *args):
            return super().send_method(method, params, self.guard(callback) if callback else None, *args)

    upstream.KlippyWebsocket = BoundSocket

    class DashboardWindow(Base):
        def __init__(self, args):
            self.kdj_store = store
            self.kdj_edit = None
            self.kdj_oe_pending = None
            self.kdj_pad = None
            self.kdj_motion = Motion(lambda text: GLib.idle_add(self.kdj_message, text))
            self.kdj_active = None
            self.kdj_endpoint = None
            self.kdj_modal = False
            self.kdj_switching = False
            self.kdj_failover = False
            self.kdj_ticks = 0
            super().__init__(args)
            self.set_title("KlipperController")
            css = Gtk.CssProvider()
            css.load_from_path(str(source / "jogger" / "style.css"))
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            self.base_panel.show_printer_select(True)
            try:
                self.kdj_pad = Gamepad(store.data["gamepad"], self.kdj_action, self.kdj_sample, self.kdj_motion.disarm)
            except Exception:
                self.kdj_message("Gamepad unavailable. Touch and keyboard controls are still available.")
            self.connect("focus-out-event", lambda *a: self.kdj_motion.disarm())
            self.connect("destroy", self.kdj_close)
            GLib.timeout_add(25, self.kdj_poll)

        def initial_connection(self):
            # Populate upstream Printer objects without auto-connecting to a placeholder.
            from ks_includes.printer import Printer
            self.printers = self._config.get_printers() if store.printers else []
            callbacks = {s: getattr(self, "state_" + s) for s in
                         ("disconnected", "error", "paused", "printing", "ready", "startup", "shutdown")}
            for p in self.printers:
                p["data"] = Printer(self.state_execute, callbacks)
            self.show_printer_select()

        @staticmethod
        def _load_panel(panel):
            from . import ui
            mapping = {
                "printer_select": ui.Dashboard,
                "kdj_discovery": ui.Discovery,
                "kdj_connection": ui.Connection,
                "kdj_octoeverywhere": ui.OctoEverywhereSetup,
                "kdj_gamepad": ui.GamepadSetup,
            }
            if panel in mapping:
                return types.SimpleNamespace(Panel=mapping[panel])
            return Base._load_panel(panel)

        def show_panel(self, panel, *args, **kwargs):
            self.kdj_motion.disarm()
            result = super().show_panel(panel, *args, **kwargs)
            self.base_panel.show_printer_select(True)
            return result

        def show_printer_select(self, widget=None):
            self.kdj_motion.disarm()
            # The dashboard is just a view; keep a healthy printer connection alive
            # so returning to that printer is instantaneous.
            self.kdj_switching = False
            super().show_printer_select(widget)

        def kdj_apply_endpoint(self, name, selected):
            """Update upstream's in-memory printer target before it opens a socket."""
            u = urlsplit(selected["url"])
            for entry in self.printers:
                if name not in entry:
                    continue
                cfg = entry[name]
                cfg["moonraker_host"] = f"[{u.hostname}]" if ":" in (u.hostname or "") else u.hostname
                cfg["moonraker_port"] = u.port or (443 if u.scheme == "https" else 80)
                cfg["moonraker_ssl"] = u.scheme == "https"
                cfg["moonraker_path"] = u.path.strip("/")
                return

        def connect_printer(self, name):
            self.kdj_motion.disarm()
            p = next((p for p in store.printers if p["name"] == name), None)
            if not p:
                return

            # Returning from the dashboard to the printer that is already connected
            # must not tear down and rebuild the websocket.
            same_live_printer = (
                self.state.printer_name == name
                and self.state.connected
                and self.state.initialized
                and self._ws is not None
                and self._ws.connected
                and not self._ws.closing
            )
            if same_live_printer:
                self.kdj_active = p
                self.kdj_motion.reset(Client(p, self.kdj_endpoint or select_endpoint(p)))
                self.show_panel("main_menu", remove_all=True)
                return

            if self.kdj_switching or self.kdj_failover or self.state.connecting or self.kdj_motion.busy:
                self.kdj_message("Wait for the current move or connection to finish, then switch.")
                return

            self.kdj_start_connection(name, p, select_endpoint(p))

        def kdj_start_connection(self, name, p, selected):
            self.kdj_motion.reset()
            self.kdj_active = p
            self.kdj_endpoint = selected
            self.kdj_apply_endpoint(name, selected)
            self.kdj_switching = True

            # Detach old callbacks before closing. Old websocket callbacks can
            # arrive after the replacement connection has started.
            old = self._ws
            if old:
                old._callback = {}
                old.callback_table.clear()
                old.close()

            if self.printer is not None:
                try:
                    self.printer.stop_tempstore_updates()
                except Exception:
                    pass

            self._ws = None
            self.server_info = None
            self.state.connected = False
            self.state.connecting = False
            self.state.initialized = False
            self.state.reinit_count = 0
            self.state.klippy_retry_count = 0
            self.last_error = ""

            super().connect_printer(name)
            self.kdj_motion.reset(Client(p, selected))

        def _finish_init(self):
            super()._finish_init()
            self.kdj_switching = False

        def socket_disconnected(self, status):
            self.kdj_motion.disarm()
            was_initialized = self.state.initialized
            p = next(
                (p for p in store.printers if p["name"] == self.state.printer_name),
                None,
            )

            # A dual-access profile should not keep retrying a dead transport.
            # Re-evaluate LAN reachability in a worker, then rebuild the websocket
            # on local Moonraker or the saved OctoEverywhere App Connection.
            if (was_initialized and p and p.get("octoeverywhere") and
                    "printer_select" not in self._cur_panels and
                    not self.kdj_switching and not self.kdj_failover):
                self.kdj_failover = True
                self.server_info = None
                self.state.connected = False
                self.state.connecting = True
                self.state.initialized = False
                if self.printer is not None:
                    self.printer.state = "disconnected"
                self.printer_initializing(
                    "Connection lost · selecting local or OctoEverywhere",
                    go_to_splash=True,
                )

                def worker():
                    selected = select_endpoint(p)
                    GLib.idle_add(self.kdj_finish_failover, p["name"], p, selected)

                threading.Thread(target=worker, daemon=True).start()
                return

            self.kdj_switching = False
            super().socket_disconnected(status)

        def kdj_finish_failover(self, name, p, selected):
            self.kdj_failover = False
            self.state.connecting = False
            self.kdj_switching = False
            self.kdj_start_connection(name, p, selected)
            return False

        def kdj_message(self, text):
            self.show_popup_message(text, level=1)
            return False

        def kdj_sample(self, held, vector):
            allowed = (self.is_active() and not self.kdj_modal and not self.dialogs and
                       self.keyboard is None and self.lock_screen.lock_box is None and
                       self.state.connected and self.state.initialized and
                       bool(self._cur_panels) and self._cur_panels[-1] == "move")

            step = 1.0
            speed_xy = 50.0
            speed_z = 10.0
            panel = self.panels.get("move")
            if panel is not None:
                try:
                    step = float(panel.distance)
                except (AttributeError, TypeError, ValueError):
                    pass
                try:
                    speed_xy = float(panel.options["move_speed_xy"].get_value())
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
                try:
                    speed_z = float(panel.options["move_speed_z"].get_value())
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass

            self.kdj_motion.sample(
                held, vector, allowed,
                step=step, speed_xy=speed_xy, speed_z=speed_z,
            )

        def kdj_poll(self):
            if self.kdj_pad:
                self.kdj_pad.poll()
            self.kdj_ticks += 1
            if self.kdj_ticks % 4 == 0:
                self.kdj_motion.tick()
            return True

        def kdj_action(self, action):
            if action == "none" or not self.is_active() or self.lock_screen.lock_box is not None:
                return
            if self.kdj_modal or self.keyboard is not None or self.dialogs:
                return
            if self._cur_panels and self._cur_panels[-1] == "kdj_gamepad":
                return  # Mapping and testing buttons must never execute them.
            self.kdj_motion.disarm()
            if action in ("next", "previous"):
                names = [p["name"] for p in store.printers]
                if names:
                    current = names.index(self.state.printer_name) if self.state.printer_name in names else -1
                    self.connect_printer(names[(current + (1 if action == "next" else -1)) % len(names)])
                return
            if action == "dashboard":
                self.show_printer_select()
                return
            if not self.state.connected or not self.state.initialized:
                self.kdj_message("Connect a ready printer first.")
                return
            if action in ("move", "temperature", "gcode_macros", "print"):
                self.show_panel("gcodes" if action == "print" else action)
                return
            ws = self._ws  # Capture the destination when the button is pressed.
            if action.startswith("macro:"):
                import re
                name = action[6:]
                if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                    self.kdj_confirm(f"{self.state.printer_name}\nRun {name}?",
                                     lambda: ws.api.gcode_script(name) if self._ws is ws and ws.connected else None)
                return
            if action == "estop":
                ws.api.emergency_stop()
            elif action == "pause":
                ws.api.print_pause()
            elif action in ("resume", "cancel", "home", "cooldown"):
                messages = {"resume": "Resume this print?", "cancel": "Cancel this print?",
                            "home": "Home all axes? Make sure the travel area is clear.",
                            "cooldown": "Turn off all heaters?"}

                def perform():
                    if self._ws is not ws or not ws.connected:
                        return
                    if action == "resume":
                        ws.api.print_resume()
                    elif action == "cancel":
                        ws.api.print_cancel()
                    elif action == "home":
                        if self.printer.state in ("printing", "paused"):
                            self.kdj_message("Homing is disabled during printing or pause.")
                        else:
                            ws.api.gcode_script("G28")
                    else:
                        ws.api.gcode_script("TURN_OFF_HEATERS")
                self.kdj_confirm(self.state.printer_name + "\n" + messages[action], perform)

        def kdj_confirm(self, text, callback):
            self.kdj_motion.disarm()
            self.kdj_modal = True
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            response = dialog.run()
            dialog.destroy()
            self.kdj_modal = False
            self.kdj_motion.disarm()
            if response == Gtk.ResponseType.OK:
                callback()

        def _key_press_event(self, widget, event):
            key = Gdk.keyval_name(event.keyval)
            if self.keyboard is not None or isinstance(self.get_focus(), Gtk.Entry):
                return False
            ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
            if ctrl and key in ("Tab", "ISO_Left_Tab"):
                self.kdj_action("previous" if event.state & Gdk.ModifierType.SHIFT_MASK else "next")
                return True
            if key == "F1":
                self.kdj_action("dashboard")
                return True
            if key == "F2":
                self.kdj_action("move")
                return True
            if event.state & Gdk.ModifierType.MOD1_MASK and key in "123456789":
                index = int(key) - 1
                if index < len(store.printers) and not self.kdj_modal and self.lock_screen.lock_box is None:
                    self.connect_printer(store.printers[index]["name"])
                return True
            if key == "Escape":
                self.kdj_motion.disarm()
            return super()._key_press_event(widget, event)

        def kdj_restart(self):
            self.kdj_motion.reset()
            # Don't interrupt a request with uncertain completion.
            if self.kdj_motion.busy:
                self.kdj_message("Wait for the current move to finish before reloading.")
                return
            if self.kdj_pad:
                self.kdj_pad.close()
            os.execv(sys.executable, [sys.executable, str(source / "launch.py")])

        def kdj_close(self, *args):
            self.kdj_motion.reset()
            if self.kdj_pad:
                self.kdj_pad.close()

    return DashboardWindow

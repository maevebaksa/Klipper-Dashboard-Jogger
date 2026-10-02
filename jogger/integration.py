"""Small adapter to the pinned upstream window; no upstream files are patched."""
import logging
import os
import sys
import types
import threading
import time
from urllib.parse import urlsplit
import websocket
from gi.repository import Gdk, GLib, Gtk, Pango
from .motion import Motion
from .network import Client, local_endpoint, select_endpoint
from .gamepad import Gamepad
from .status import StatusMonitor
from . import filaments
from . import watchdog
from . import updater

# While a dual-access printer is on OctoEverywhere, look for the LAN this often.
# Each check is one small /server/info request per LAN address; it must exceed
# LOCAL_DEADLINE_S in network.py so checks never overlap.
LAN_RECHECK_S = 30
# Panels where rebuilding the connection loses nothing the operator is doing.
SAFE_SWITCH_PANELS = {"main_menu", "job_status"}
ROUTE_TEXT = {"local": "the local network", "octoeverywhere": "OctoEverywhere", "remote": "the remote URL"}
# Gamepad presses have no key release to commit on, so the switcher picks the
# highlighted printer after this pause. It must exceed a comfortable gap
# between repeated presses (about half a second) so several printers can be
# stepped through, while staying short enough to feel immediate.
SWITCHER_COMMIT_S = 1.5
# While Ctrl + Tab is held, check the physical Ctrl key this often instead of
# trusting a key-release event: with no window manager, X focus can follow the
# pointer onto the popup, so the release (and a focus-out) may never reach the
# main window. 50 ms is below what feels like a delay after letting go.
SWITCHER_CTRL_POLL_MS = 50
# The status monitor only fetches printers whose own interval has elapsed
# (status.LOCAL_REFRESH_S / REMOTE_REFRESH_S); this tick just has to be finer.
STATUS_TICK_S = 5
# After initialization, how long to let KlipperScreen's own state dispatch
# move off the splash screen before doing it ourselves. The normal dispatch is
# an idle callback queued during initialization, so it runs within
# milliseconds; this must comfortably exceed that.
STATE_KICK_MS = 1500


def load_filament_panel_from(path, fallback):
    """Import Klipper Filament Sync's KlipperScreen panel from its checkout.

    The panel is the plugin's own file, run unmodified, the way the plugin's
    installer links it into KlipperScreen's panels directory.
    """
    import importlib.util
    if not os.path.isfile(path):
        return types.SimpleNamespace(Panel=fallback)
    spec = importlib.util.spec_from_file_location("kdj_" + filaments.PANEL, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_window(Base, store, source):
    import screen as upstream
    # App data directory: the managed KlipperScreen checkout lives inside it.
    data_dir = os.path.dirname(os.path.dirname(os.path.abspath(upstream.__file__)))

    def load_filament_panel():
        from . import ui
        return load_filament_panel_from(filaments.panel_path(data_dir), ui.FilamentsMissing)
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
            # Upstream's retry timer can fire while an attempt is still in
            # flight; a second WebSocketApp on this object would deliver every
            # message twice. A hung attempt is replaced by the stall watchdog.
            if self.connected or self.connecting:
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
            # Keepalive: a half-open connection is closed instead of lingering.
            self._wst = threading.Thread(target=watchdog.run_forever, args=(self.ws,), daemon=True)
            try:
                self._wst.start()
            except Exception:
                return True
            return False

        def on_open(self, *args):
            self.mark_progress()
            return super().on_open(*args)

        def on_message(self, *args):
            self.mark_progress()
            return super().on_message(*args)

        def mark_progress(self):
            # Runs on the websocket thread; a float store is safe to share.
            if self.owner._ws is self:
                self.owner.kdj_last_progress = time.monotonic()

        def send_method(self, method, params=None, callback=None, *args):
            return super().send_method(method, params, self.guard(callback) if callback else None, *args)

    upstream.KlippyWebsocket = BoundSocket

    class DashboardWindow(Base):
        def __init__(self, args):
            self.kdj_store = store
            self.kdj_source = source
            self.kdj_upstream = os.path.dirname(os.path.abspath(upstream.__file__))
            # The commit this process loaded, to tell when an update pulled
            # new code that is not running yet.
            self.kdj_started_head = updater.head(source)
            self.kdj_edit = None
            self.kdj_oe_pending = None
            self.kdj_pad = None
            self.kdj_motion = Motion(lambda text: GLib.idle_add(self.kdj_message, text))
            self.kdj_active = None
            self.kdj_endpoint = None
            self.kdj_modal = False
            self.kdj_switching = False
            self.kdj_failover = False
            self.kdj_connection_generation = 0
            self.kdj_ticks = 0
            self.kdj_lan_checking = False
            self.kdj_next_lan_check = 0.0
            self.kdj_announced_source = None
            self.kdj_switcher = None
            self.kdj_switch_hold = False
            self.kdj_switch_timer = None
            self.kdj_ctrl_timer = None
            self.kdj_ctrl_seen = False
            self.kdj_last_progress = time.monotonic()
            self.kdj_stall_restarts = 0
            self.kdj_stall_reported = False
            self.kdj_next_stall_check = 0.0
            # Before super().__init__: upstream shows the dashboard during init.
            self.kdj_status = StatusMonitor(
                lambda: store.printers, lambda: GLib.idle_add(self.kdj_status_changed))
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
            self.connect("focus-out-event", self.kdj_focus_out)
            self.connect("key-release-event", self.kdj_key_release)
            self.connect("destroy", self.kdj_close)
            GLib.timeout_add(25, self.kdj_poll)
            GLib.timeout_add_seconds(STATUS_TICK_S, self.kdj_status.poll)
            GLib.idle_add(lambda: self.kdj_status.poll() and False)
            if updater.take_result_screen_request(os.path.dirname(self.kdj_upstream)):
                # A failed update restarted us to load its new code; show why it failed.
                GLib.timeout_add_seconds(2, lambda: self.show_panel("kdj_update") and False)

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
                "kdj_manage": ui.Manage,
                "kdj_ssh": ui.SshPanel,
                "kdj_discovery": ui.Discovery,
                "kdj_connection": ui.Connection,
                "kdj_octoeverywhere": ui.RemoteLink,
                "kdj_gamepad": ui.GamepadSetup,
                "kdj_update": ui.UpdatePanel,
            }
            if panel in mapping:
                return types.SimpleNamespace(Panel=mapping[panel])
            if panel == filaments.PANEL:
                return load_filament_panel()
            return Base._load_panel(panel)

        def show_panel(self, panel, *args, **kwargs):
            self.kdj_motion.disarm()
            result = super().show_panel(panel, *args, **kwargs)
            self.base_panel.show_printer_select(True)
            return result

        def show_printer_select(self, widget=None):
            self.kdj_motion.disarm()
            # The dashboard is just a view; keep a healthy established printer
            # connection alive, but cancel any unfinished endpoint selection/failover.
            if self.kdj_switching or self.kdj_failover:
                self.kdj_connection_generation += 1
            self.kdj_switching = False
            self.kdj_failover = False
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

            # A motion request is the only operation we do not supersede. It has
            # bounded HTTP timeouts and clears Motion.busy in finally.
            if self.kdj_motion.busy:
                self.kdj_message("Wait for the current jog to finish, then switch.")
                return

            same_live_printer = (
                self.state.printer_name == name
                and self.state.connected
                and self.state.initialized
                and self._ws is not None
                and self._ws.connected
                and not self._ws.closing
            )
            if same_live_printer:
                self.kdj_connection_generation += 1
                self.kdj_switching = False
                self.kdj_failover = False
                self.kdj_active = p
                self.kdj_motion.reset(Client(p, self.kdj_endpoint or {
                    "url": p["url"], "remote": p.get("remote", False),
                    "source": "current", "authorization": ""
                }))
                self.show_panel("main_menu", remove_all=True)
                return

            # New selections supersede an unfinished connection instead of leaving
            # the UI trapped behind "wait for connection". Endpoint probing happens
            # off the GTK thread so a dead LAN address cannot freeze the dashboard.
            self.kdj_connection_generation += 1
            generation = self.kdj_connection_generation
            self.kdj_switching = True
            self.kdj_failover = False
            self.kdj_stall_restarts = 0
            self.kdj_stall_reported = False

            def worker():
                try:
                    selected = select_endpoint(p)
                except Exception:
                    selected = {"url": p["url"], "remote": False, "source": "local", "authorization": ""}
                GLib.idle_add(
                    self.kdj_selected_endpoint,
                    generation, name, p, selected,
                )

            threading.Thread(target=worker, daemon=True).start()

        def kdj_selected_endpoint(self, generation, name, p, selected):
            if generation != self.kdj_connection_generation:
                return False
            self.kdj_start_connection(name, p, selected, generation)
            return False

        def kdj_start_connection(self, name, p, selected, generation=None):
            if generation is not None and generation != self.kdj_connection_generation:
                return
            self.kdj_motion.reset()
            self.kdj_active = p
            self.kdj_endpoint = selected
            self.kdj_apply_endpoint(name, selected)
            self.kdj_switching = True
            self.kdj_next_lan_check = time.monotonic() + LAN_RECHECK_S
            self.kdj_last_progress = time.monotonic()
            if selected.get("problem"):
                self.kdj_message(f"{name}: local network unavailable. {selected['problem']}")

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
            # Detaching the old callbacks skips upstream socket_disconnected,
            # which is what marks a printer "disconnected". KlipperScreen only
            # leaves the splash screen on a state *change*, so a printer
            # revisited quickly would still read "ready" and initialization
            # would finish without ever leaving "Initializing Klipper
            # Connection". Reset every cached state, as a disconnect would.
            for entry in getattr(self, "printers", None) or []:
                cached = entry.get("data")
                if cached is not None and hasattr(cached, "state"):
                    cached.state = "disconnected"

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
            self.kdj_stall_restarts = 0
            self.kdj_stall_reported = False
            GLib.timeout_add(STATE_KICK_MS, self.kdj_kick_state, self.kdj_connection_generation)
            # Say which route is in use when it is not the usual LAN, and when
            # a printer comes back to the LAN from a remote route.
            name = self.state.printer_name
            source = (self.kdj_endpoint or {}).get("source", "local")
            previous = self.kdj_announced_source or (None, "local")
            back_on_lan = previous[0] == name and previous[1] != "local"
            if source != "local" or back_on_lan:
                self.kdj_message(f"{name}: connected through {ROUTE_TEXT.get(source, source)}.")
            self.kdj_announced_source = (name, source)

        def socket_disconnected(self, status):
            self.kdj_motion.disarm()
            was_initialized = self.state.initialized
            p = next(
                (p for p in store.printers if p["name"] == self.state.printer_name),
                None,
            )

            # A dual-access profile should not keep retrying a dead transport.
            # Re-evaluate LAN reachability in a worker, then rebuild the websocket
            # on local Moonraker, its LAN fallback address, or OctoEverywhere.
            has_alternative = bool(p and (p.get("octoeverywhere") or p.get("lan_fallback_url")))
            if (was_initialized and has_alternative and
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

                generation = self.kdj_connection_generation

                def worker():
                    try:
                        selected = select_endpoint(p)
                    except Exception:
                        selected = {"url": p["url"], "remote": False, "source": "local", "authorization": ""}
                    GLib.idle_add(
                        self.kdj_finish_failover,
                        generation, p["name"], p, selected,
                    )

                threading.Thread(target=worker, daemon=True).start()
                return

            self.kdj_switching = False
            self.kdj_failover = False
            super().socket_disconnected(status)

        def kdj_finish_failover(self, generation, name, p, selected):
            if generation != self.kdj_connection_generation:
                return False
            self.kdj_failover = False
            self.state.connecting = False
            self.kdj_switching = False
            self.kdj_start_connection(name, p, selected, generation)
            return False

        def kdj_check_lan(self):
            """While on OctoEverywhere, return to the LAN once it answers again."""
            endpoint = self.kdj_endpoint or {}
            p = self.kdj_active
            if (not endpoint.get("remote") or not p or not p.get("octoeverywhere") or
                    self.kdj_lan_checking or self.kdj_switching or self.kdj_failover or
                    not (self.state.connected and self.state.initialized)):
                return
            self.kdj_lan_checking = True
            generation = self.kdj_connection_generation

            def worker():
                selected = None
                try:
                    selected = local_endpoint(p)
                finally:
                    GLib.idle_add(self.kdj_lan_result, generation, p, selected)

            threading.Thread(target=worker, daemon=True).start()

        def kdj_lan_result(self, generation, p, selected):
            self.kdj_lan_checking = False
            if not selected or generation != self.kdj_connection_generation:
                return False
            top = self._cur_panels[-1] if self._cur_panels else ""
            if (self.kdj_motion.busy or self.kdj_modal or self.dialogs or
                    self.keyboard is not None or top not in SAFE_SWITCH_PANELS):
                # Try again at the next check instead of pulling the operator
                # out of a panel they are using.
                return False
            self.kdj_connection_generation += 1
            self.kdj_start_connection(p["name"], p, selected, self.kdj_connection_generation)
            return False

        def kdj_kick_state(self, generation):
            """Safety net: initialized but still on the splash screen, dispatch the state.

            Normally the state change queued while initializing moves off the
            splash screen within a moment. If none was queued (the state did
            not change), the operator would be stuck on "Initializing Klipper
            Connection" with a working connection.
            """
            if (generation == self.kdj_connection_generation and self.state.initialized
                    and self.printer is not None and self._cur_panels
                    and self._cur_panels[-1] == "splash_screen"):
                state = self.printer.evaluate_state()
                if state in ("ready", "printing", "paused", "error", "shutdown", "startup"):
                    logging.info("[kdj] %s: initialized but still on the splash screen; "
                                 "dispatching state %s", self.state.printer_name, state)
                    self.printer.change_state(state)
            return False

        def kdj_check_stall(self):
            """Restart a printer connection that stopped making progress (see watchdog.py)."""
            p = self.kdj_active
            top = self._cur_panels[-1] if self._cur_panels else ""
            if (p is None or self.state.initialized or self.kdj_failover or self._ws is None
                    or top in ("", "printer_select") or self.state.printer_name != p["name"]):
                return
            remote = bool((self.kdj_endpoint or {}).get("remote"))
            if not watchdog.stalled(time.monotonic(), self.kdj_last_progress, remote):
                return
            name = p["name"]
            if self.kdj_stall_restarts >= watchdog.MAX_STALL_RESTARTS:
                if not self.kdj_stall_reported:
                    self.kdj_stall_reported = True
                    logging.info("[kdj] %s: no response after %d reconnects; giving up", name,
                                 self.kdj_stall_restarts)
                    self.printer_initializing(
                        f"{name} is not responding.\n\nCheck the printer and its network, then "
                        "press F1 and tap it on the dashboard to try again.", go_to_splash=True)
                return
            self.kdj_stall_restarts += 1
            logging.info("[kdj] %s: no progress for %ss; reconnecting (%d/%d)", name,
                         watchdog.REMOTE_STALL_S if remote else watchdog.LOCAL_STALL_S,
                         self.kdj_stall_restarts, watchdog.MAX_STALL_RESTARTS)
            self.kdj_reconnect(p, f"No response from {name} · reconnecting "
                                  f"({self.kdj_stall_restarts}/{watchdog.MAX_STALL_RESTARTS})")

        def kdj_reconnect(self, p, message):
            """Re-pick local or OctoEverywhere and rebuild the connection."""
            self.kdj_failover = True
            self.kdj_last_progress = time.monotonic()
            self.printer_initializing(message, go_to_splash=True)
            generation = self.kdj_connection_generation

            def worker():
                try:
                    selected = select_endpoint(p)
                except Exception:
                    selected = {"url": p["url"], "remote": False, "source": "local", "authorization": ""}
                GLib.idle_add(self.kdj_finish_failover, generation, p["name"], p, selected)

            threading.Thread(target=worker, daemon=True).start()

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
            now = time.monotonic()
            if now >= self.kdj_next_lan_check:
                self.kdj_next_lan_check = now + LAN_RECHECK_S
                self.kdj_check_lan()
            if now >= self.kdj_next_stall_check:
                self.kdj_next_stall_check = now + 1
                self.kdj_check_stall()
            return True

        def kdj_status_changed(self):
            if self._cur_panels and self._cur_panels[-1] == "printer_select":
                panel = self.panels.get("printer_select")
                if hasattr(panel, "kdj_status_changed"):
                    panel.kdj_status_changed()
            if self.kdj_switcher is not None and self.kdj_switcher.visible:
                self.kdj_switcher.update()
            return False

        def kdj_switch(self, delta, hold):
            """Open the switcher or move its highlight; see ui.Switcher."""
            names = [p["name"] for p in store.printers]
            if not names:
                self.kdj_message("No printers saved yet. Open Manage to add one.")
                return
            from . import ui
            if self.kdj_switcher is None:
                self.kdj_switcher = ui.Switcher(self)
            if self.kdj_switcher.visible and self.kdj_switcher.names == names:
                self.kdj_switcher.step(delta)
            else:
                current = names.index(self.state.printer_name) if self.state.printer_name in names else -1
                start = current + delta if current >= 0 else (0 if delta > 0 else -1)
                self.kdj_switcher.open(names, start, hold)
                self.kdj_status.poll()
            self.kdj_switch_hold = hold
            self.kdj_switcher.set_hold(hold)
            self.kdj_cancel_switch_timer()
            if not hold:
                self.kdj_switch_timer = GLib.timeout_add(
                    int(SWITCHER_COMMIT_S * 1000), self.kdj_switch_commit)
            elif self.kdj_ctrl_timer is None:
                self.kdj_ctrl_seen = False
                self.kdj_ctrl_timer = GLib.timeout_add(SWITCHER_CTRL_POLL_MS, self.kdj_check_ctrl)

        def kdj_ctrl_held(self):
            """True/False for the physical Ctrl key, or None if it cannot be read."""
            try:
                keymap = Gdk.Keymap.get_for_display(self.get_display())
                return bool(keymap.get_modifier_state() & Gdk.ModifierType.CONTROL_MASK)
            except Exception:
                return None

        def kdj_check_ctrl(self):
            if not (self.kdj_switch_hold and self.kdj_switching_visible()):
                self.kdj_ctrl_timer = None
                return False
            held = self.kdj_ctrl_held()
            if held is None:
                self.kdj_ctrl_timer = None  # fall back to key-release events
                return False
            if held:
                self.kdj_ctrl_seen = True
                return True
            if self.kdj_ctrl_seen:
                # Released. Clear the id first: commit cancels timers, and this
                # source ends by returning False.
                self.kdj_ctrl_timer = None
                self.kdj_switch_commit()
                return False
            return True

        def kdj_cancel_switch_timer(self):
            if self.kdj_switch_timer is not None:
                GLib.source_remove(self.kdj_switch_timer)
                self.kdj_switch_timer = None

        def kdj_switch_commit(self):
            self.kdj_switch_timer = None
            if self.kdj_switcher is not None and self.kdj_switcher.visible:
                self.kdj_switch_to(self.kdj_switcher.selected())
            return False

        def kdj_switch_to(self, name):
            self.kdj_switch_cancel()
            if name:
                self.connect_printer(name)

        def kdj_switch_cancel(self):
            self.kdj_cancel_switch_timer()
            if self.kdj_ctrl_timer is not None:
                GLib.source_remove(self.kdj_ctrl_timer)
                self.kdj_ctrl_timer = None
            self.kdj_switch_hold = False
            if self.kdj_switcher is not None:
                self.kdj_switcher.close()

        def kdj_switching_visible(self):
            return self.kdj_switcher is not None and self.kdj_switcher.visible

        def kdj_focus_out(self, *args):
            # Not a reason to close the switcher: showing its popup can itself
            # move X focus. The Ctrl poll decides when a held switch ends.
            self.kdj_motion.disarm()
            return False

        def kdj_refresh_dashboard(self):
            panel = self.panels.get("printer_select")
            if hasattr(panel, "refresh"):
                panel.refresh()

        def kdj_key_release(self, widget, event):
            key = Gdk.keyval_name(event.keyval)
            if self.kdj_switch_hold and key in ("Control_L", "Control_R") and self.kdj_switching_visible():
                self.kdj_switch_commit()
                return True
            return False

        def kdj_action(self, action, hold=False):
            # While the switcher is open, X focus may sit on its popup (no window
            # manager), leaving this window inactive; keys still come from the
            # operator via the popup, so do not drop them.
            active = self.is_active() or self.kdj_switching_visible()
            if action == "none" or not active or self.lock_screen.lock_box is not None:
                return
            if self.kdj_modal or self.keyboard is not None or self.dialogs:
                return
            if self._cur_panels and self._cur_panels[-1] == "kdj_gamepad":
                return  # Mapping and testing buttons must never execute them.
            self.kdj_motion.disarm()
            if action in ("next", "previous"):
                self.kdj_switch(1 if action == "next" else -1, hold)
                return
            if self.kdj_switching_visible():
                self.kdj_switch_cancel()
            if action == "dashboard":
                self.show_printer_select()
                return
            if not self.state.connected or not self.state.initialized:
                self.kdj_message("Connect a ready printer first.")
                return
            if action in ("move", "temperature", "gcode_macros", "print"):
                self.show_panel("gcodes" if action == "print" else action)
                return
            if action == "filaments":
                macros = [m.lower() for m in self.printer.get_gcode_macros()] if self.printer else []
                if filaments.MACRO.lower() in macros:
                    self.show_panel(filaments.PANEL)
                else:
                    self.kdj_message(f"{self.state.printer_name} does not have Klipper Filament Sync.")
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
            """Ask Yes/No on KlipperScreen's own themed dialog.

            A stock Gtk.MessageDialog drew KlipperScreen's white text on
            Adwaita's light dialog background, so these prompts (install,
            cancel print, home, macros) were unreadable.
            """
            self.kdj_motion.disarm()
            self.kdj_modal = True
            answered = []

            def release(*_args):
                # Also runs when upstream destroys open dialogs on a panel
                # change without a response; the gamepad must not stay locked.
                if self.kdj_modal:
                    self.kdj_modal = False
                    self.kdj_motion.disarm()

            def respond(dialog, response_id):
                if answered:
                    return
                answered.append(response_id)
                self.gtk.remove_dialog(dialog)
                release()
                if response_id == Gtk.ResponseType.OK:
                    callback()

            prompt = Gtk.Label(label=text, hexpand=True, vexpand=True, halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER, justify=Gtk.Justification.CENTER, wrap=True)
            prompt.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            buttons = [
                {"name": "Yes", "response": Gtk.ResponseType.OK, "style": "dialog-info"},
                {"name": "No", "response": Gtk.ResponseType.CANCEL, "style": "dialog-error"},
            ]
            dialog = self.gtk.Dialog("KlipperController", buttons, prompt, respond)
            dialog.connect("destroy", release)

        def _key_press_event(self, widget, event):
            key = Gdk.keyval_name(event.keyval)
            if self.keyboard is not None or isinstance(self.get_focus(), Gtk.Entry):
                return False
            ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
            focus = self.get_focus()
            in_terminal = getattr(focus, "kdj_terminal", False) or type(focus).__name__ == "Terminal"
            if in_terminal and not self.kdj_switching_visible():
                # The SSH terminal gets every key except the two global shortcuts.
                if not ((ctrl and key in ("Tab", "ISO_Left_Tab")) or key == "F1"):
                    return False
            top = self._cur_panels[-1] if self._cur_panels else ""
            if ctrl and key in ("r", "R") and top == "printer_select":
                self.kdj_refresh_dashboard()
                return True
            if ctrl and key in ("Tab", "ISO_Left_Tab"):
                back = key == "ISO_Left_Tab" or event.state & Gdk.ModifierType.SHIFT_MASK
                self.kdj_action("previous" if back else "next", hold=True)
                return True
            if self.kdj_switching_visible():
                digit = key[3:] if key.startswith("KP_") else key
                if digit in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    # Jump straight to a printer by the number on its tile.
                    names = self.kdj_switcher.names
                    if int(digit) <= len(names):
                        self.kdj_switch_to(names[int(digit) - 1])
                elif key == "Escape":
                    self.kdj_switch_cancel()
                elif key in ("Return", "KP_Enter", "space"):
                    self.kdj_switch_commit()
                elif key in ("Right", "Down"):
                    self.kdj_switch(1, self.kdj_switch_hold)
                elif key in ("Left", "Up"):
                    self.kdj_switch(-1, self.kdj_switch_hold)
                return True
            digit = key[3:] if key.startswith("KP_") else key
            plain = not (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK))
            if (top == "printer_select" and plain and digit in ("1", "2", "3", "4", "5", "6", "7", "8", "9")
                    and not self.kdj_modal and not self.dialogs and self.lock_screen.lock_box is None):
                # On the dashboard, a printer's number (shown on its tile) opens it.
                index = int(digit) - 1
                if index < len(store.printers):
                    self.connect_printer(store.printers[index]["name"])
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

        def remove_keyboard(self, entry=None, event=None, box=None):
            # Our panels pack KlipperScreen's keyboard into their own content,
            # but upstream (Back, Home, F1) removes it from base_panel.content
            # unless told otherwise. That removed the keys and left the empty
            # half-screen keyboard frame in the panel. Remove it from wherever
            # it actually is.
            if box is None and self.keyboard is not None and self.keyboard.get("box") is not None:
                parent = self.keyboard["box"].get_parent()
                if parent is not None:
                    box = parent
            return super().remove_keyboard(entry, event, box)

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

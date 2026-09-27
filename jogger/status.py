"""Live printer status shared by the dashboard and the printer switcher.

No GTK here, so it is testable. The window owns one StatusMonitor and ticks
it; each printer is refreshed on its own schedule so the switcher always has
recent data, even while a KlipperScreen panel is open.
"""
import concurrent.futures
import threading
import time

from .network import Client, ConnectionError, select_endpoint

# LAN checks are two small requests per printer (endpoint probe, status query).
# OctoEverywhere checks cross the relay, so they run less often to respect its
# limits.
LOCAL_REFRESH_S = 15
REMOTE_REFRESH_S = 60
MAX_PARALLEL_CHECKS = 4

LABELS = {
    "ready": "Ready", "printing": "Printing", "paused": "Paused", "complete": "Finished",
    "cancelled": "Cancelled", "error": "Error", "shutdown": "Klipper shut down",
    "startup": "Klipper starting", "offline": "Offline", "auth": "Needs sign-in",
    "remote_problem": "Remote problem", "checking": "Checking…",
}
ROUTES = {"local": "local network", "octoeverywhere": "OctoEverywhere", "remote": "remote"}


# Broad groups drive tile colors and the dashboard summary line.
GROUPS = {
    "printing": "printing", "paused": "paused", "startup": "paused",
    "complete": "done", "ready": "ready", "cancelled": "ready",
    "error": "problem", "shutdown": "problem", "auth": "problem", "remote_problem": "problem",
    "offline": "offline", "checking": "checking",
}
SUMMARY_ORDER = (("printing", "printing"), ("paused", "paused"), ("problem", "need attention"),
                 ("offline", "offline"), ("done", "finished"), ("ready", "ready"))


def group(state):
    return GROUPS.get(state, "offline")


def fleet_summary(statuses):
    """One line such as '2 printing · 1 paused · 1 offline'."""
    statuses = list(statuses)
    if not statuses:
        return "No printers yet."
    counts = {}
    for s in statuses:
        counts[group(s["state"])] = counts.get(group(s["state"]), 0) + 1
    if counts.get("checking") == len(statuses):
        return "Checking printers…"
    return " · ".join(f"{counts[key]} {text}" for key, text in SUMMARY_ORDER if counts.get(key))


def checking():
    return {"state": "checking", "label": LABELS["checking"], "progress": None,
            "filename": "", "route": "", "remote": False, "detail": ""}


def _problem(state, detail="", remote=False):
    return dict(checking(), state=state, label=LABELS[state], detail=detail, remote=remote)


def fetch_status(printer):
    """One printer's current status. Never raises."""
    try:
        selected = select_endpoint(printer)
    except Exception:
        return _problem("offline")
    remote = bool(selected.get("remote"))
    client = Client(printer, selected, timeout=(3, 8) if remote else (1.5, 3))
    try:
        summary = client.summary()
    except ConnectionError as exc:
        message = str(exc)
        if "Authorization" in message:
            return _problem("auth", "Needs its API key or a new remote link.", remote)
        if "OctoEverywhere" in message:
            return _problem("remote_problem", message, remote)
        return _problem("offline", "", bool(printer.get("octoeverywhere")))
    except Exception:
        return _problem("offline")
    finally:
        client.close()

    state = summary["state"]
    if state == "standby":
        state = "ready"
    active = state in ("printing", "paused")
    return {
        "state": state,
        "label": LABELS.get(state, state.capitalize()),
        "progress": summary.get("progress") if active else None,
        "filename": summary.get("filename", "") if active or state == "complete" else "",
        "route": selected.get("source", ""),
        "remote": remote,
        "detail": "",
    }


def headline(status):
    """Short state text such as 'Printing 42%'."""
    text = status["label"]
    if status.get("progress") is not None:
        text += f" {round(status['progress'] * 100)}%"
    return text


def route_text(status):
    route = status.get("route")
    return f"via {ROUTES.get(route, route)}" if route else ""


class StatusMonitor:
    """Refresh due printers in the background and report the whole map.

    printers: callable returning the saved printer dicts.
    notify: called with no arguments, from a worker thread, after new results
    are stored; the GTK side hops to the main loop itself.
    """

    def __init__(self, printers, notify, fetch=fetch_status, clock=time.monotonic):
        self.printers = printers
        self.notify = notify
        self.fetch = fetch
        self.clock = clock
        self.statuses = {}
        self.next_due = {}
        self.lock = threading.Lock()
        self.running = False

    def get(self, name):
        with self.lock:
            return dict(self.statuses.get(name) or checking())

    def snapshot(self):
        with self.lock:
            return {name: dict(value) for name, value in self.statuses.items()}

    def refresh_now(self, name=None):
        """Make one printer (or all) due at the next poll."""
        with self.lock:
            if name is None:
                self.next_due.clear()
            else:
                self.next_due.pop(name, None)

    def due(self):
        now = self.clock()
        printers = [dict(p) for p in self.printers()]
        names = {p["name"] for p in printers}
        with self.lock:
            for gone in set(self.statuses) - names:
                self.statuses.pop(gone, None)
                self.next_due.pop(gone, None)
            return [p for p in printers if self.next_due.get(p["name"], 0) <= now]

    def poll(self, wait=False):
        """Start a background refresh of due printers. Returns True for GLib timers."""
        with self.lock:
            if self.running:
                return True
            self.running = True
        due = self.due()
        if not due:
            with self.lock:
                self.running = False
            return True

        def worker():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHECKS) as pool:
                    results = list(zip(due, pool.map(self.fetch, due)))
                now = self.clock()
                with self.lock:
                    for printer, status in results:
                        self.statuses[printer["name"]] = status
                        interval = REMOTE_REFRESH_S if status.get("remote") else LOCAL_REFRESH_S
                        self.next_due[printer["name"]] = now + interval - 1
            finally:
                with self.lock:
                    self.running = False
            self.notify()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True

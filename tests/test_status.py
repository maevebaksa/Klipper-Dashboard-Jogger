import threading

import pytest

from jogger import status
from jogger.network import ConnectionError
from jogger.status import StatusMonitor, fetch_status, headline, route_text


class FakeClient:
    result = None

    def __init__(self, printer, selected, timeout=None):
        self.selected = selected

    def summary(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    route = {"url": "http://p:7125", "remote": False, "source": "local", "authorization": ""}
    monkeypatch.setattr(status, "select_endpoint", lambda p: dict(route))
    monkeypatch.setattr(status, "Client", FakeClient)
    return route


def test_printing_status_has_progress_file_and_route(fake):
    FakeClient.result = {"state": "printing", "progress": 0.426, "filename": "bracket.gcode"}
    s = fetch_status({"name": "Voron"})
    assert headline(s) == "Printing 43%"
    assert s["filename"] == "bracket.gcode"
    assert route_text(s) == "via local network"


def test_standby_reads_as_ready_without_stale_progress(fake):
    FakeClient.result = {"state": "standby", "progress": 1.0, "filename": "old.gcode"}
    s = fetch_status({"name": "Voron"})
    assert headline(s) == "Ready"
    assert s["progress"] is None and s["filename"] == ""


@pytest.mark.parametrize("error, state", [
    (ConnectionError("Connection failed or timed out; verify address."), "offline"),
    (ConnectionError("Authorization needed: add a Moonraker API key."), "auth"),
    (ConnectionError("OctoEverywhere timed out reaching the printer."), "remote_problem"),
    (RuntimeError("boom"), "offline"),
])
def test_errors_become_states_and_never_raise(fake, error, state):
    FakeClient.result = error
    assert fetch_status({"name": "Voron"})["state"] == state


def test_remote_route_label(fake):
    fake.update(remote=True, source="octoeverywhere")
    FakeClient.result = {"state": "paused", "progress": 0.5, "filename": "x"}
    s = fetch_status({"name": "Voron"})
    assert s["remote"] and route_text(s) == "via OctoEverywhere"


def test_monitor_schedules_remote_printers_less_often():
    clock = [1000.0]
    printers = [{"name": "Near"}, {"name": "Far"}]
    fetched = []

    def fetch(p):
        fetched.append(p["name"])
        return dict(status.checking(), state="ready", label="Ready", remote=p["name"] == "Far")
    notified = threading.Event()
    monitor = StatusMonitor(lambda: printers, notified.set, fetch=fetch, clock=lambda: clock[0])

    monitor.poll(wait=True)
    assert sorted(fetched) == ["Far", "Near"] and notified.is_set()
    assert monitor.get("Near")["state"] == "ready"

    fetched.clear()
    clock[0] += status.LOCAL_REFRESH_S
    monitor.poll(wait=True)
    assert fetched == ["Near"]  # Far is on the slower remote schedule

    fetched.clear()
    clock[0] += status.REMOTE_REFRESH_S
    monitor.poll(wait=True)
    assert sorted(fetched) == ["Far", "Near"]


def test_monitor_forgets_removed_printers_and_defaults_to_checking():
    printers = [{"name": "A"}]
    monitor = StatusMonitor(lambda: printers, lambda: None,
                            fetch=lambda p: dict(status.checking(), state="ready"))
    monitor.poll(wait=True)
    printers.clear()
    monitor.poll(wait=True)
    assert monitor.snapshot() == {}
    assert monitor.get("A")["state"] == "checking"


def test_fleet_summary():
    def s(state):
        return dict(status.checking(), state=state)
    assert status.fleet_summary([]) == "No printers yet."
    assert status.fleet_summary([s("checking"), s("checking")]) == "Checking printers…"
    assert status.fleet_summary(
        [s("printing"), s("printing"), s("error"), s("standby_unknown"), s("ready"), s("checking")]
    ) == "2 printing · 1 need attention · 1 offline · 1 ready"
    assert status.group("auth") == "problem"


def test_refresh_requested_mid_run_is_not_lost():
    started, release = threading.Event(), threading.Event()
    fetched = []

    def fetch(p):
        fetched.append(p["name"])
        if len(fetched) == 1:
            started.set()
            release.wait(5)
        return status.checking()
    monitor = StatusMonitor(lambda: [{"name": "A"}], lambda: None, fetch=fetch)
    monitor.poll()
    started.wait(5)
    monitor.refresh_now()
    monitor.poll()  # arrives while the first refresh is still running
    release.set()
    for _ in range(100):
        if len(fetched) == 2 and not monitor.running:
            break
        threading.Event().wait(0.02)
    assert fetched == ["A", "A"]


def test_refresh_now_makes_a_printer_due_again():
    clock = [0.0]
    fetched = []
    monitor = StatusMonitor(lambda: [{"name": "A"}], lambda: None,
                            fetch=lambda p: fetched.append(1) or status.checking(),
                            clock=lambda: clock[0])
    monitor.poll(wait=True)
    monitor.poll(wait=True)
    assert len(fetched) == 1
    monitor.refresh_now("A")
    monitor.poll(wait=True)
    assert len(fetched) == 2

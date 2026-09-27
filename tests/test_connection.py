import configparser
import json
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from jogger.config import Store, endpoint, profile
from jogger.network import (
    Client, ConnectionError, _add_discovery, _hostname_candidate, clean_name,
    lan_fallback_url, local_endpoint, select_endpoint, verify_remote,
)
from jogger.octoeverywhere import error_message


def test_urls_and_private_save(tmp_path):
    assert endpoint("printer.local:7125") == "http://printer.local:7125"
    assert endpoint("https://example.com/moonraker/websocket") == "https://example.com:443/moonraker"
    assert endpoint("http://[::1]:7125") == "http://[::1]:7125"
    store = Store(tmp_path)
    store.put(profile("Voron", "https://secret.octoeverywhere.com/prefix", "a%secret"))
    assert store.printers[0]["remote"]
    cfg = configparser.ConfigParser()
    cfg.read(store.generate())
    assert cfg["printer Voron"]["moonraker_path"] == "prefix"
    assert cfg["printer Voron"]["moonraker_api_key"] == "a%secret"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert len(Store(tmp_path).printers) == 1


@pytest.mark.parametrize("url", ["ftp://a", "http://a\nb", "http://u:p@a", "https://a?token=secret", "http://a:bad"])
def test_reject_invalid_urls(url):
    with pytest.raises(ValueError):
        endpoint(url)


@pytest.fixture
def server():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("X-Api-Key")))
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            if self.headers.get("X-Api-Key") != "secret":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"result": {"klippy_connected": True}}).encode())
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", seen
    http.shutdown()
    http.server_close()


def test_http_path_auth_and_no_login_redirect(server):
    url, seen = server
    client = Client(profile("Test", url + "/prefix", "secret"))
    assert client.test() == "Connected"
    assert seen[-1] == ("/prefix/server/info", "secret")
    with pytest.raises(ConnectionError, match="Authorization"):
        Client(profile("Test", url)).test()
    with pytest.raises(ConnectionError, match="redirects"):
        Client(profile("Test", url + "/redirect", "secret")).test()
    assert all(path != "/login" for path, _ in seen)


def test_dual_access_prefers_lan_then_octoeverywhere(monkeypatch):
    printer = profile("Voron", "http://voron.local:7125")
    printer["octoeverywhere"] = {
        "url": "https://app-test.octoeverywhere.com",
        "auth": {"type": "bearer", "token": "secret-token"},
    }

    monkeypatch.setattr("jogger.network._check_endpoint", lambda *args, **kwargs: (True, ""))
    local = select_endpoint(printer)
    assert local["source"] == "local"
    assert local["url"] == "http://voron.local:7125"
    assert not local["remote"]

    def only_remote(_printer, candidate, _timeout):
        return (candidate["remote"], "")
    monkeypatch.setattr("jogger.network._check_endpoint", only_remote)
    remote = select_endpoint(printer)
    assert remote["source"] == "octoeverywhere"
    assert remote["url"] == "https://app-test.octoeverywhere.com"
    assert remote["authorization"] == "Bearer secret-token"
    assert remote["remote"]
    assert "problem" not in remote


def test_lan_ip_fallback_beats_remote(monkeypatch):
    printer = profile("Voron", "http://voron.local:7125")
    printer["lan_fallback_url"] = "http://192.168.1.20:7125"
    printer["octoeverywhere"] = {"kind": "shared", "url": "https://shared-x.octoeverywhere.com", "auth": {}}
    monkeypatch.setattr(
        "jogger.network._check_endpoint",
        lambda _p, c, _t: (c["url"] != "http://voron.local:7125", ""),
    )
    chosen = select_endpoint(printer)
    assert chosen["url"] == "http://192.168.1.20:7125"
    assert chosen["source"] == "local"
    assert local_endpoint(printer)["url"] == "http://192.168.1.20:7125"


def test_hung_lan_lookup_does_not_block_remote(monkeypatch):
    # A .local lookup can block past the requests timeout; the deadline must win.
    monkeypatch.setattr("jogger.network.LOCAL_DEADLINE_S", 0.2)
    printer = profile("Voron", "http://voron.local:7125")
    printer["octoeverywhere"] = {"kind": "shared", "url": "https://shared-x.octoeverywhere.com", "auth": {}}
    release = threading.Event()

    def check(_p, candidate, _t):
        if not candidate["remote"]:
            release.wait(5)
            return False, ""
        return True, ""
    monkeypatch.setattr("jogger.network._check_endpoint", check)
    started = time.monotonic()
    chosen = select_endpoint(printer)
    release.set()
    assert chosen["source"] == "octoeverywhere"
    assert time.monotonic() - started < 2


def test_unreachable_everywhere_reports_octoeverywhere_problem(monkeypatch):
    printer = profile("Voron", "http://voron.local:7125")
    printer["octoeverywhere"] = {"kind": "app", "url": "https://app-x.octoeverywhere.com",
                                 "auth": {"type": "bearer", "token": "t"}}
    monkeypatch.setattr(
        "jogger.network._check_endpoint",
        lambda _p, c, _t: (False, error_message(605) if c["remote"] else ""),
    )
    chosen = select_endpoint(printer)
    assert chosen["source"] == "octoeverywhere"
    assert "Supporter Perks" in chosen["problem"]
    assert local_endpoint(printer) is None


@pytest.fixture
def moonraker():
    """A fake Moonraker whose routes the test fills in: path -> (status, body)."""
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, body = routes.get(self.path, (404, {"error": {"code": 404, "message": "not found"}}))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    routes["/server/info"] = (200, {"result": {"klippy_connected": True}})
    yield f"http://127.0.0.1:{http.server_port}", routes
    http.shutdown()
    http.server_close()


MAINSAIL = "/server/database/item?namespace=mainsail&key=general.printername"
FLUIDD = "/server/database/item?namespace=fluidd&key=uiSettings.general.instanceName"
PRINTER_INFO = "/printer/info"
OE_ID = "/server/database/item?namespace=octoeverywhere&key=public.printerId"


def local_client(url):
    return Client(profile("T", url), {"url": url, "remote": False, "source": "local"})


def test_details_prefers_mainsail_then_fluidd_then_hostname(moonraker):
    url, routes = moonraker
    routes[PRINTER_INFO] = (200, {"result": {"hostname": "voron24", "state": "ready"}})
    routes[OE_ID] = (200, {"result": {"namespace": "octoeverywhere", "key": "public.printerId", "value": "OEID"}})
    client = local_client(url)

    assert client.details() == {"name": "voron24", "hostname": "voron24", "oe_printer_id": "OEID"}

    routes[FLUIDD] = (200, {"result": {"value": "Fluidd"}})  # front-end default, not a name
    assert client.details()["name"] == "voron24"

    routes[FLUIDD] = (200, {"result": {"value": "Workshop Voron"}})
    assert client.details()["name"] == "Workshop Voron"

    routes[MAINSAIL] = (200, {"result": {"value": "Voron 2.4 \U0001f680 / 350"}})
    assert client.details()["name"] == "Voron 2.4 350"


def test_details_never_raises_when_klippy_is_down(moonraker):
    url, routes = moonraker
    routes[PRINTER_INFO] = (503, {"error": {"code": 503, "message": "Klippy Disconnected"}})
    assert local_client(url).details() == {"name": "", "hostname": "", "oe_printer_id": ""}


def test_octoeverywhere_status_codes_become_readable_errors(moonraker):
    url, routes = moonraker
    client = Client(profile("T", url), {"url": url, "remote": True, "source": "octoeverywhere"})
    routes["/server/info"] = (605, {})
    with pytest.raises(ConnectionError, match="Supporter Perks"):
        client.server_info()
    routes["/server/info"] = (604, {})
    with pytest.raises(ConnectionError, match="revoked"):
        client.server_info()


def test_summary_reports_print_progress(moonraker):
    url, routes = moonraker
    routes["/printer/objects/query?webhooks&print_stats&display_status"] = (200, {"result": {"status": {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "printing", "filename": "part.gcode"},
        "display_status": {"progress": 0.42},
    }}})
    assert local_client(url).summary() == {"state": "printing", "progress": 0.42, "filename": "part.gcode"}


def test_verify_remote_rejects_a_different_printer(moonraker, monkeypatch):
    local_url, routes = moonraker
    remote_url = local_url + "/remote"
    routes[OE_ID] = (200, {"result": {"value": "PRINTER-A"}})
    routes["/remote/server/info"] = (200, {"result": {"klippy_connected": True}})
    routes["/remote" + OE_ID] = (200, {"result": {"value": "PRINTER-B"}})
    printer = profile("Voron", local_url)
    oe = {"kind": "shared", "url": "https://shared-x.octoeverywhere.com", "auth": {}}

    real_client = Client

    def routed_client(p, selected, timeout=None):
        # Send the "remote" side to a path on the same fake server.
        if selected["remote"]:
            selected = dict(selected, url=remote_url)
        return real_client(p, selected, timeout)
    monkeypatch.setattr("jogger.network.Client", routed_client)

    with pytest.raises(ConnectionError, match="different printer"):
        verify_remote(printer, oe)
    routes["/remote" + OE_ID] = (200, {"result": {"value": "printer-a"}})
    assert verify_remote(printer, oe)["oe_printer_id"] == "printer-a"

    routes["/remote/server/info"] = (401, {})
    with pytest.raises(ConnectionError, match="username and password"):
        verify_remote(printer, oe)


def test_lan_fallback_url_only_for_private_hostname_urls():
    assert lan_fallback_url("http://voron.local:7125", "192.168.1.20") == "http://192.168.1.20:7125"
    assert lan_fallback_url("http://192.168.1.20:7125", "192.168.1.20") == ""
    assert lan_fallback_url("http://voron.local:7125", "8.8.8.8") == ""
    assert clean_name("  ..My/Printer!! ") == "My Printer"


def test_hostname_candidate_and_discovery_deduplication():
    assert (
        _hostname_candidate("http://192.168.1.20:7125", "voron24")
        == "http://voron24.local:7125"
    )
    found = {}
    _add_discovery(found, {
        "url": "http://voron24.local:80",
        "name": "voron24",
        "identity": "voron24",
        "protected": False,
    })
    _add_discovery(found, {
        "url": "http://voron24.local:7125",
        "name": "voron24",
        "identity": "voron24",
        "protected": False,
    })
    assert len(found) == 1
    assert found["voron24"]["url"] == "http://voron24.local:7125"

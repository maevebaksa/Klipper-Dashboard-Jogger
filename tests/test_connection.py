import configparser
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from jogger.config import Store, endpoint, profile
from jogger.network import Client, ConnectionError


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

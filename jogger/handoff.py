"""One-time local web page for finishing remote-access setup on a phone.

Typing a long OctoEverywhere URL on a touchscreen is error-prone, and a login
page inside an embedded browser cannot use the phone's password manager. The
touchscreen instead shows a QR code for a short-lived page on this Pi. The
user opens it on a phone on the same network and pastes the Shared Connection
URL (or finishes the App Connection portal, which returns to this page).

Safety limits: an unguessable one-time path, private-network clients only, a
small request size cap, a failed-attempt cap, an expiry, and no-referrer and
no-store headers so the path does not leak into the OctoEverywhere site or a
browser cache.
"""
import html
import ipaddress
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .octoeverywhere import SHARED_CONNECTIONS_URL

HANDOFF_TTL_S = 15 * 60          # long enough to sign in and create a share link
MAX_BODY_BYTES = 8 * 1024        # a URL plus two short credential fields
MAX_BAD_REQUESTS = 25            # stop serving after this many wrong paths/rejects

_STYLE = """
body{font-family:system-ui,sans-serif;background:#121922;color:#edf2f8;margin:0;padding:18px;line-height:1.4}
main{max-width:520px;margin:auto}h1{font-size:1.3em}a.button,button{display:block;width:100%;box-sizing:border-box;
text-align:center;padding:14px;margin:10px 0;border-radius:10px;border:1px solid #70cbbb;background:#285c58;
color:#edf2f8;font-size:1em;text-decoration:none}textarea,input{width:100%;box-sizing:border-box;padding:10px;
border-radius:8px;border:1px solid #38495f;background:#0f1620;color:#f1f5fb;font-size:1em}
label{display:block;margin-top:12px}.muted{color:#a4b1c2;font-size:.9em}.ok{color:#84d5c4}.err{color:#ff9d9d}
ol{padding-left:20px}details{margin-top:12px}
"""


def lan_address():
    """This Pi's LAN IPv4 address, without sending any packets."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routing lookup only
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def _private_client(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class Handoff:
    """Serve one setup page until it succeeds, expires or is stopped.

    mode "shared": the page shows a paste form; on_submit(fields) receives a dict
    with url, username and password and returns (ok, message).
    mode "app": the page redirects to portal_url(return_url) and on_submit
    receives {"completion_url": ...} when the portal returns.
    on_submit runs on a server thread and may block on network checks.
    """

    def __init__(self, mode, on_submit, printer_name="", portal_url=None,
                 ttl=HANDOFF_TTL_S, host="", bind="0.0.0.0", port=0):
        self.mode = mode
        self.on_submit = on_submit
        self.printer_name = printer_name
        self.portal_url = portal_url
        self.token = secrets.token_urlsafe(9)
        self.expires = time.monotonic() + ttl
        self.done = False
        self.bad_requests = 0
        self._lock = threading.Lock()
        self._host = host
        self._bind = (bind, port)
        self._server = None

    @property
    def url(self):
        host = self._host or lan_address() or "127.0.0.1"
        return f"http://{host}:{self._server.server_port}/{self.token}"

    def start(self):
        handoff = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "KlipperController"
            sys_version = ""

            def log_message(self, *args):
                pass  # the path is a secret; never log it

            def do_GET(self):
                handoff._handle(self, "GET")

            def do_POST(self):
                handoff._handle(self, "POST")

        self._server = ThreadingHTTPServer(self._bind, Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.url

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()),
                             daemon=True).start()

    # Request handling -----------------------------------------------------

    def _reject(self, request, code, text):
        with self._lock:
            self.bad_requests += 1
            too_many = self.bad_requests >= MAX_BAD_REQUESTS
        self._send(request, code, self._page("Not available", f'<p class="err">{html.escape(text)}</p>'))
        if too_many:
            self.stop()

    def _handle(self, request, method):
        if not _private_client(request.client_address[0]):
            return self._reject(request, 403, "Only devices on the local network can use this page.")
        if time.monotonic() > self.expires:
            return self._reject(request, 410, "This setup link expired. Start again on the touchscreen.")
        parts = urlsplit(request.path)
        base = "/" + self.token
        if parts.path not in (base, base + "/complete"):
            return self._reject(request, 404, "Unknown page.")
        if self.done:
            return self._send(request, 200, self._page(
                "Already linked", '<p class="ok">Remote access is linked. You can close this page.</p>'))

        if self.mode == "app":
            if parts.path == base and method == "GET":
                return self._redirect(request, self.portal_url(self.url + "/complete"))
            if parts.path == base + "/complete" and method == "GET":
                ok, message = self.on_submit({"completion_url": "https://localhost" + request.path})
                return self._result(request, ok, message)
            return self._reject(request, 405, "Unsupported request.")

        if parts.path != base:
            return self._reject(request, 404, "Unknown page.")
        if method == "GET":
            return self._send(request, 200, self._form())
        length = int(request.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._reject(request, 413, "That request is too large.")
        body = request.rfile.read(length).decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        ok, message = self.on_submit({
            "url": fields.get("url", ""),
            "username": fields.get("username", ""),
            "password": fields.get("password", ""),
        })
        if ok:
            return self._result(request, True, message)
        return self._send(request, 200, self._form(error=message, value=fields.get("url", "")))

    def _result(self, request, ok, message):
        if ok:
            self.done = True
            body = f'<p class="ok">{html.escape(message)}</p><p class="muted">You can close this page.</p>'
            self._send(request, 200, self._page("Linked", body))
        else:
            self._send(request, 200, self._page("Not linked", f'<p class="err">{html.escape(message)}</p>'))

    def _form(self, error="", value=""):
        printer = html.escape(self.printer_name or "this printer")
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""
<p>Link OctoEverywhere remote access for <b>{printer}</b>.</p>
<ol>
<li>Open OctoEverywhere Shared Connections and sign in.</li>
<li>Create a Shared Connection for {printer}, or pick an existing one.</li>
<li>Copy its URL, come back here, and paste it below.</li>
</ol>
<a class="button" href="{SHARED_CONNECTIONS_URL}" target="_blank" rel="noopener noreferrer">Open OctoEverywhere Shared Connections</a>
{err}
<form method="post" autocomplete="off">
<label for="url">Shared Connection URL</label>
<textarea id="url" name="url" rows="3" required placeholder="https://....octoeverywhere.com">{html.escape(value)}</textarea>
<details><summary>My shared connection uses a username and password</summary>
<label for="username">Username</label><input id="username" name="username">
<label for="password">Password</label><input id="password" name="password" type="password">
</details>
<button type="submit">Link remote access</button>
</form>
<p class="muted">The link is checked against the printer before it is saved on the controller.</p>"""
        return self._page("Link remote access", body)

    @staticmethod
    def _page(title, body):
        return (f'<!doctype html><html><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta name="referrer" content="no-referrer">'
                f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
                f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>")

    @staticmethod
    def _headers(request):
        request.send_header("Cache-Control", "no-store")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header("X-Frame-Options", "DENY")

    def _send(self, request, code, page):
        data = page.encode("utf-8")
        request.send_response(code)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(data)))
        self._headers(request)
        request.end_headers()
        request.wfile.write(data)

    def _redirect(self, request, location):
        request.send_response(302)
        request.send_header("Location", location)
        request.send_header("Content-Length", "0")
        self._headers(request)
        request.end_headers()

"""Moonraker HTTP and bounded mDNS/LAN discovery. Never logs credential URLs."""
import concurrent.futures
import ipaddress
import json
import socket
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
import requests
from .config import endpoint
from .octoeverywhere import authorization_header


class ConnectionError(RuntimeError):
    pass


class Client:
    def __init__(self, printer, selected=None):
        selected = selected or select_endpoint(printer)
        self.url = selected["url"]
        self.remote = selected["remote"]
        self.source = selected["source"]
        self.session = requests.Session()
        self.session.trust_env = False
        if printer.get("api_key"):
            self.session.headers["X-Api-Key"] = printer["api_key"]
        if selected.get("authorization"):
            self.session.headers["Authorization"] = selected["authorization"]

    def request(self, route, payload=None):
        try:
            response = self.session.request("GET" if payload is None else "POST", self.url + route,
                                            json=payload, timeout=(3, 8 if self.remote else 3),
                                            allow_redirects=False)
            if response.status_code in (401, 403):
                raise ConnectionError("Authorization needed: add a Moonraker API key or reauthorize OctoEverywhere remote access.")
            if 300 <= response.status_code < 400:
                raise ConnectionError("This address redirects to a login page and cannot be used as a Moonraker endpoint.")
            response.raise_for_status()
            data = response.json()
            if "error" in data or "result" not in data:
                raise ConnectionError("Moonraker rejected the request. Check the printer console.")
            return data["result"]
        except (requests.RequestException, ValueError) as exc:
            raise ConnectionError("Connection failed or timed out; verify address, network and printer state.") from None

    def server_info(self):
        data = self.request("/server/info")
        if "klippy_connected" not in data:
            raise ConnectionError("The address did not return Moonraker server information.")
        return data

    def test(self):
        data = self.server_info()
        return "Connected" if data["klippy_connected"] else "Moonraker found · Klipper offline"

    def octoeverywhere_printer_id(self):
        data = self.request(
            "/server/database/item?namespace=octoeverywhere&key=public.printerId"
        )
        printer_id = data.get("value") if isinstance(data, dict) else None
        if not isinstance(printer_id, str) or not printer_id.strip():
            raise ConnectionError("OctoEverywhere is not advertising a printer ID through Moonraker.")
        return printer_id.strip()

    def status(self):
        return self.request("/printer/objects/query?toolhead&print_stats&webhooks&pause_resume&virtual_sdcard&gcode_move")["status"]

    def gcode(self, script):
        return self.request("/printer/gcode/script", {"script": script})

    def close(self):
        self.session.close()



def _can_reach(printer, url, authorization="", timeout=(0.35, 0.75)):
    headers = {}
    if printer.get("api_key"):
        headers["X-Api-Key"] = printer["api_key"]
    if authorization:
        headers["Authorization"] = authorization
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url.rstrip("/") + "/server/info",
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            if response.status_code != 200:
                return False
            data = response.json()
            return "klippy_connected" in data.get("result", {})
    except (requests.RequestException, ValueError):
        return False


def select_endpoint(printer):
    """Prefer a reachable LAN Moonraker URL, otherwise use its App Connection."""
    local = printer["url"]
    oe = printer.get("octoeverywhere") or {}

    # Legacy profiles that are themselves remote keep their old behavior.
    if printer.get("remote") and not oe.get("url"):
        return {"url": local, "remote": True, "source": "remote", "authorization": ""}

    if _can_reach(printer, local):
        return {"url": local, "remote": False, "source": "local", "authorization": ""}

    if oe.get("url"):
        return {
            "url": oe["url"],
            "remote": True,
            "source": "octoeverywhere",
            "authorization": authorization_header(printer),
        }

    # Let KlipperScreen surface the normal connection error if LAN probing
    # failed and no cloud endpoint exists.
    return {"url": local, "remote": False, "source": "local", "authorization": ""}



def _hostname_candidate(url, hostname):
    """Build a stable hostname URL from Moonraker's reported host name."""
    hostname = (hostname or "").strip().rstrip(".")
    if not hostname:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")
    if any(ch not in allowed for ch in hostname):
        return None
    parsed = urlsplit(url)
    if not parsed.hostname:
        return None
    host = hostname if "." in hostname else hostname + ".local"
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _server_info_at(url, timeout=(0.35, 0.7), headers=None):
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url.rstrip("/") + "/server/info",
                headers=headers or {},
                timeout=timeout,
                allow_redirects=False,
            )
            if response.status_code != 200:
                return None
            data = response.json().get("result", {})
            return data if "klippy_connected" in data else None
    except (requests.RequestException, ValueError):
        return None


def prefer_hostname_url(printer, info):
    """Prefer hostname.local over a discovered numeric IP when it resolves."""
    current = printer["url"]
    candidate = _hostname_candidate(current, info.get("hostname"))
    if not candidate or candidate == current:
        return current
    headers = {}
    if printer.get("api_key"):
        headers["X-Api-Key"] = printer["api_key"]
    if _server_info_at(candidate, timeout=(0.35, 0.8), headers=headers):
        return endpoint(candidate)
    return current


def _probe_detail(url):
    info = _server_info_at(url)
    if info:
        hostname = (info.get("hostname") or "").strip()
        preferred = endpoint(url)
        candidate = _hostname_candidate(preferred, hostname)
        if candidate and _server_info_at(candidate):
            preferred = endpoint(candidate)
        return {
            "url": preferred,
            "name": hostname or (urlsplit(preferred).hostname or "Moonraker"),
            "identity": (hostname or (urlsplit(preferred).hostname or preferred)).lower().rstrip("."),
            "protected": False,
        }

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url.rstrip("/") + "/server/info",
                timeout=(0.35, 0.7),
                allow_redirects=False,
            )
            if response.status_code in (401, 403):
                host = urlsplit(url).hostname or url
                return {
                    "url": endpoint(url),
                    "name": "Protected Moonraker",
                    "identity": host.lower().rstrip("."),
                    "protected": True,
                }
    except requests.RequestException:
        pass
    return None


def probe(url):
    detail = _probe_detail(url)
    return (detail["url"], detail["name"]) if detail else None


def _discovery_rank(detail):
    parsed = urlsplit(detail["url"])
    host = parsed.hostname or ""
    try:
        numeric = ipaddress.ip_address(host)
        host_penalty = 1 if numeric else 0
    except ValueError:
        host_penalty = 0
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    port_penalty = 0 if port == 7125 else (1 if port in (80, 443) else 2)
    protected_penalty = 1 if detail.get("protected") else 0
    return (host_penalty, port_penalty, protected_penalty)


def _add_discovery(found, detail):
    if not detail:
        return
    key = detail["identity"]
    existing = found.get(key)
    if existing is None or _discovery_rank(detail) < _discovery_rank(existing):
        found[key] = detail


def local_hosts():
    """At most 254 hosts per directly attached IPv4 interface; no routed network scans."""
    try:
        interfaces = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr", "show", "up"], timeout=3))
        hosts = set()
        for iface in interfaces:
            if iface["ifname"] == "lo" or iface["ifname"].startswith(("docker", "veth", "tun", "wg")):
                continue
            for a in iface.get("addr_info", []):
                if a.get("scope") == "global":
                    net = ipaddress.ip_network(f'{a["local"]}/{max(24, a["prefixlen"])}', strict=False)
                    hosts.update(str(h) for h in net.hosts())
        return sorted(hosts)[:508]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []



def discover(scan=False):
    """Discover each Moonraker instance once, preferring hostname:7125 over IP/proxy duplicates."""
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    found = {}

    class Listener(ServiceListener):
        def add_service(self, zc, kind, name):
            info = zc.get_service_info(kind, name, timeout=800)
            if not info:
                return
            stable_host = (info.server or "").rstrip(".")
            service_name = name.removesuffix("._moonraker._tcp.local.")
            if stable_host:
                url = endpoint(f"http://{stable_host}:{info.port}")
                detail = _probe_detail(url) or {
                    "url": url,
                    "name": service_name,
                    "identity": stable_host.lower().removesuffix(".local"),
                    "protected": False,
                }
                _add_discovery(found, detail)
                return
            for address in info.parsed_addresses():
                if ":" not in address:
                    detail = _probe_detail(endpoint(f"http://{address}:{info.port}"))
                    _add_discovery(found, detail)

        update_service = add_service

        def remove_service(self, *args):
            pass

    zc = Zeroconf()
    browser = ServiceBrowser(zc, "_moonraker._tcp.local.", Listener())
    try:
        time.sleep(2.5)
    finally:
        browser.cancel()
        zc.close()

    if scan:
        urls = (f"http://{host}:{port}" for host in local_hosts() for port in (7125, 80))
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            for detail in pool.map(_probe_detail, urls):
                _add_discovery(found, detail)

    rows = [(item["url"], item["name"]) for item in found.values()]
    return sorted(rows, key=lambda item: item[1].lower())

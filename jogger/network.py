"""Moonraker HTTP and bounded mDNS/LAN discovery. Never logs credential URLs."""
import concurrent.futures
import ipaddress
import json
import re
import socket
import subprocess
import time
from urllib.parse import quote, urlsplit, urlunsplit
import requests
from .config import endpoint
from .octoeverywhere import authorization_header, error_message

# LAN probes: a Pi on Wi-Fi can take well over 350 ms for the first packet after
# idle, so the connect timeout must exceed that. requests' timeout does not bound
# getaddrinfo, and a failing .local lookup can block for seconds, so the whole
# local check also has a wall-clock deadline.
LOCAL_PROBE_TIMEOUT = (1.0, 1.5)
LOCAL_DEADLINE_S = 3.0
# The OctoEverywhere relay adds a cloud round trip and a printer-side hop.
REMOTE_PROBE_TIMEOUT = (4.0, 8.0)
REMOTE_DEADLINE_S = 10.0

# User-visible printer names that front ends keep in Moonraker's database, in
# the order we prefer them. Verified against upstream sources:
# Mainsail src/store/gui (namespace "mainsail", general.printername) and
# Fluidd src/globals.ts + store/config (namespace "fluidd",
# uiSettings.general.instanceName).
NAME_SOURCES = (
    ("mainsail", "general.printername"),
    ("fluidd", "uiSettings.general.instanceName"),
)
# Front-end defaults that are not a real printer name.
_DEFAULT_UI_NAMES = {"fluidd", "mainsail", "klipper", "moonraker"}


class ConnectionError(RuntimeError):
    pass


def clean_name(text):
    """Fit a printer-reported name to the saved profile name rules."""
    text = re.sub(r"[^\w .()-]+", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip(" .")[:48].strip()


class Client:
    def __init__(self, printer, selected=None, timeout=None):
        selected = selected or select_endpoint(printer)
        self.url = selected["url"]
        self.remote = selected["remote"]
        self.source = selected["source"]
        self.timeout = timeout or ((3, 8) if self.remote else (3, 3))
        self.session = requests.Session()
        self.session.trust_env = False
        if printer.get("api_key"):
            self.session.headers["X-Api-Key"] = printer["api_key"]
        if selected.get("authorization"):
            self.session.headers["Authorization"] = selected["authorization"]

    def request(self, route, payload=None):
        try:
            response = self.session.request("GET" if payload is None else "POST", self.url + route,
                                            json=payload, timeout=self.timeout,
                                            allow_redirects=False)
        except requests.RequestException:
            raise ConnectionError("Connection failed or timed out; verify address, network and printer state.") from None
        oe_problem = error_message(response.status_code)
        if oe_problem:
            raise ConnectionError(oe_problem)
        if response.status_code in (401, 403):
            raise ConnectionError("Authorization needed: add a Moonraker API key or relink OctoEverywhere remote access.")
        if 300 <= response.status_code < 400:
            raise ConnectionError("This address redirects to a login page and cannot be used as a Moonraker endpoint.")
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            raise ConnectionError("The printer returned an unexpected response.") from None
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            raise ConnectionError("Moonraker rejected the request. Check the printer console.")
        return data["result"]

    def server_info(self):
        data = self.request("/server/info")
        if "klippy_connected" not in data:
            raise ConnectionError("The address did not return Moonraker server information.")
        return data

    def test(self):
        data = self.server_info()
        return "Connected" if data["klippy_connected"] else "Moonraker found · Klipper offline"

    def db_item(self, namespace, key):
        """One Moonraker database value, or None when missing or unreadable."""
        try:
            data = self.request(
                f"/server/database/item?namespace={quote(namespace)}&key={quote(key)}"
            )
        except ConnectionError:
            return None
        return data.get("value") if isinstance(data, dict) else None

    def octoeverywhere_printer_id(self):
        # Written by the OctoEverywhere Moonraker plugin
        # (moonraker_octoeverywhere/moonrakerdatabase.py) for third-party apps.
        printer_id = self.db_item("octoeverywhere", "public.printerId")
        if not isinstance(printer_id, str) or not printer_id.strip():
            raise ConnectionError("OctoEverywhere is not advertising a printer ID through Moonraker.")
        return printer_id.strip()

    def details(self):
        """Best-effort printer name, Klipper host name and OctoEverywhere ID. Never raises."""
        found = {"name": "", "hostname": "", "oe_printer_id": ""}
        for namespace, key in NAME_SOURCES:
            value = self.db_item(namespace, key)
            name = clean_name(value) if isinstance(value, str) else ""
            if name and name.lower() not in _DEFAULT_UI_NAMES:
                found["name"] = name
                break
        try:
            # /server/info has no host name; Klipper reports it in /printer/info
            # (only while Klippy is connected).
            info = self.request("/printer/info")
            found["hostname"] = str(info.get("hostname") or "").strip()
        except ConnectionError:
            pass
        printer_id = self.db_item("octoeverywhere", "public.printerId")
        if isinstance(printer_id, str):
            found["oe_printer_id"] = printer_id.strip()
        if not found["name"]:
            found["name"] = clean_name(found["hostname"])
        return found

    def status(self):
        return self.request("/printer/objects/query?toolhead&print_stats&webhooks&pause_resume&virtual_sdcard&gcode_move")["status"]

    def filament_lanes(self):
        """Klipper Filament Sync tool filaments, or [] without the plugin. Never raises."""
        from .filaments import parse_lanes
        try:
            data = self.request("/server/database/item?namespace=lane_data")
        except ConnectionError:
            return []  # 404 when the plugin is not installed
        return parse_lanes(data.get("value") if isinstance(data, dict) else None)

    def summary(self):
        """Compact state for dashboard cards."""
        status = self.request("/printer/objects/query?webhooks&print_stats&display_status")["status"]
        webhooks = status.get("webhooks") or {}
        stats = status.get("print_stats") or {}
        display = status.get("display_status") or {}
        klippy = webhooks.get("state") or "unknown"
        state = stats.get("state") if klippy == "ready" else klippy
        return {
            "state": state or "unknown",
            "progress": display.get("progress"),
            "filename": stats.get("filename") or "",
        }

    def gcode(self, script):
        return self.request("/printer/gcode/script", {"script": script})

    def close(self):
        self.session.close()


def _check_endpoint(printer, candidate, timeout):
    """Return (reachable, problem) for one endpoint candidate."""
    headers = {}
    if printer.get("api_key"):
        headers["X-Api-Key"] = printer["api_key"]
    if candidate.get("authorization"):
        headers["Authorization"] = candidate["authorization"]
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                candidate["url"].rstrip("/") + "/server/info",
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            problem = error_message(response.status_code)
            if problem:
                return False, problem
            if response.status_code in (401, 403):
                return False, "Authorization needed"
            if response.status_code != 200:
                return False, ""
            return "klippy_connected" in response.json().get("result", {}), ""
    except (requests.RequestException, ValueError, AttributeError):
        return False, ""


def _can_reach(printer, url, authorization="", timeout=LOCAL_PROBE_TIMEOUT):
    return _check_endpoint(printer, {"url": url, "authorization": authorization}, timeout)[0]


def endpoint_candidates(printer):
    """(local, remote) candidate lists in preference order."""
    local = [{"url": printer["url"], "remote": False, "source": "local", "authorization": ""}]
    fallback = printer.get("lan_fallback_url")
    if fallback and fallback != printer["url"]:
        local.append({"url": fallback, "remote": False, "source": "local", "authorization": ""})
    remote = []
    oe = printer.get("octoeverywhere") or {}
    if oe.get("url"):
        remote.append({
            "url": oe["url"],
            "remote": True,
            "source": "octoeverywhere",
            "authorization": authorization_header(printer),
        })
    return local, remote


def _run_checks(printer, groups):
    """Probe every candidate at once; return the first reachable in priority order.

    groups is a list of (candidates, timeout, deadline_s). Deadlines are measured
    from the start, so a slow LAN check does not delay the remote check.
    """
    jobs = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, sum(len(g[0]) for g in groups)))
    started = time.monotonic()
    for candidates, timeout, deadline in groups:
        for candidate in candidates:
            jobs.append((candidate, deadline, pool.submit(_check_endpoint, printer, candidate, timeout)))
    # Never block on a hung DNS lookup; abandoned checks finish on their own.
    pool.shutdown(wait=False)
    problem = ""
    for candidate, deadline, future in jobs:
        remaining = started + deadline - time.monotonic()
        try:
            ok, why = future.result(timeout=max(0.0, remaining))
        except concurrent.futures.TimeoutError:
            ok, why = False, ""
        if ok:
            return candidate, ""
        if candidate["remote"] and why:
            problem = why
    return None, problem


def select_endpoint(printer):
    """Prefer a reachable LAN Moonraker URL, otherwise use OctoEverywhere."""
    local, remote = endpoint_candidates(printer)

    # Legacy profiles whose primary URL is itself remote keep their old behavior.
    if printer.get("remote") and not remote:
        return {"url": printer["url"], "remote": True, "source": "remote", "authorization": ""}

    groups = [(local, LOCAL_PROBE_TIMEOUT, LOCAL_DEADLINE_S)]
    if remote:
        groups.append((remote, REMOTE_PROBE_TIMEOUT, REMOTE_DEADLINE_S))
    chosen, problem = _run_checks(printer, groups)
    if chosen:
        return dict(chosen)
    if remote:
        # Hand KlipperScreen the remote endpoint so it retries and reports.
        return dict(remote[0], problem=problem or "Neither the local network nor OctoEverywhere answered.")
    return dict(local[0])


def local_endpoint(printer):
    """A reachable LAN endpoint for this printer, or None."""
    local, _remote = endpoint_candidates(printer)
    chosen, _problem = _run_checks(printer, [(local, LOCAL_PROBE_TIMEOUT, LOCAL_DEADLINE_S)])
    return dict(chosen) if chosen else None


def verify_remote(printer, oe):
    """Check a new OctoEverywhere link works and reaches this same printer.

    Returns the remote printer details, or raises ConnectionError with a
    message suitable for the phone page.
    """
    candidate = {
        "url": oe["url"], "remote": True, "source": "octoeverywhere",
        "authorization": authorization_header({"octoeverywhere": oe}),
    }
    remote = Client(printer, candidate, timeout=REMOTE_PROBE_TIMEOUT)
    try:
        remote.server_info()
        remote_details = remote.details()
    except ConnectionError as exc:
        if "Authorization" in str(exc):
            raise ConnectionError(
                "OctoEverywhere or Moonraker refused the link. If the shared connection "
                "has a username and password, enter them too."
            ) from None
        raise
    finally:
        remote.close()

    local = Client(printer, {"url": printer["url"], "remote": False, "source": "local"},
                   timeout=LOCAL_PROBE_TIMEOUT)
    try:
        local.server_info()
        local_details = local.details()
    except ConnectionError:
        local_details = None  # away from home: nothing to compare against
    finally:
        local.close()

    if local_details:
        # The OctoEverywhere printer ID is the strongest identity; fall back to
        # Klipper's host name. Compare only fields both sides reported.
        for key in ("oe_printer_id", "hostname"):
            mine, theirs = local_details[key], remote_details[key]
            if mine and theirs:
                if mine.lower() != theirs.lower():
                    raise ConnectionError(
                        "That link reaches a different printer. Pick the shared "
                        f"connection for {printer.get('name') or 'this printer'}."
                    )
                break
    return remote_details


def _hostname_candidate(url, hostname):
    """Build a stable hostname URL from Klipper's reported host name."""
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


def _is_ip(host):
    try:
        ipaddress.ip_address((host or "").strip("[]"))
        return True
    except ValueError:
        return False


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
    except (requests.RequestException, ValueError, AttributeError):
        return None


def prefer_hostname_url(printer, hostname):
    """Prefer hostname.local over a numeric IP when it answers as Moonraker."""
    current = printer["url"]
    if not _is_ip(urlsplit(current).hostname):
        return current
    candidate = _hostname_candidate(current, hostname)
    if not candidate or candidate == current:
        return current
    headers = {}
    if printer.get("api_key"):
        headers["X-Api-Key"] = printer["api_key"]
    if _server_info_at(candidate, timeout=(0.8, 1.2), headers=headers):
        return endpoint(candidate)
    return current


def lan_fallback_url(url, known_ip=""):
    """Numeric-IP twin of a hostname URL, used when mDNS stops resolving."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if not host or _is_ip(host):
        return ""
    ip = known_ip.strip()
    if not ip:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 80, socket.AF_INET, socket.SOCK_STREAM)
            ip = infos[0][4][0] if infos else ""
        except (OSError, UnicodeError):
            ip = ""
    try:
        if not ip or not ipaddress.ip_address(ip).is_private:
            return ""
    except ValueError:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return endpoint(urlunsplit((parsed.scheme, f"{ip}:{port}", parsed.path, "", "")))


def _probe_detail(url):
    info = _server_info_at(url)
    if info:
        preferred = endpoint(url)
        client = Client({}, {"url": preferred, "remote": False, "source": "local"},
                        timeout=(0.5, 1.0))
        try:
            details = client.details()
        finally:
            client.close()
        hostname = details["hostname"]
        candidate = _hostname_candidate(preferred, hostname)
        if candidate and _is_ip(urlsplit(preferred).hostname) and _server_info_at(candidate):
            preferred = endpoint(candidate)
        host = urlsplit(preferred).hostname or preferred
        return {
            "url": preferred,
            "name": details["name"] or clean_name(host) or "Moonraker",
            "identity": (hostname or host).lower().rstrip(".").removesuffix(".local"),
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
                    "identity": host.lower().rstrip(".").removesuffix(".local"),
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
    host_penalty = 1 if _is_ip(parsed.hostname) else 0
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
                    "name": clean_name(service_name) or "Moonraker",
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

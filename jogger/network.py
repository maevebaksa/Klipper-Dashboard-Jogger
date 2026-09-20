"""Moonraker HTTP and bounded mDNS/LAN discovery. Never logs credential URLs."""
import concurrent.futures
import ipaddress
import json
import socket
import subprocess
import time
import requests
from .config import endpoint


class ConnectionError(RuntimeError):
    pass


class Client:
    def __init__(self, printer):
        self.url = printer["url"]
        self.remote = printer.get("remote", False)
        self.session = requests.Session()
        self.session.trust_env = False
        if printer.get("api_key"):
            self.session.headers["X-Api-Key"] = printer["api_key"]

    def request(self, route, payload=None):
        try:
            response = self.session.request("GET" if payload is None else "POST", self.url + route,
                                            json=payload, timeout=(3, 8 if self.remote else 3),
                                            allow_redirects=False)
            if response.status_code in (401, 403):
                raise ConnectionError("Authorization needed: add a Moonraker API key or a valid custom app URL.")
            if 300 <= response.status_code < 400:
                raise ConnectionError("This URL redirects to a login page. Use an OctoEverywhere custom app connection URL.")
            response.raise_for_status()
            data = response.json()
            if "error" in data or "result" not in data:
                raise ConnectionError("Moonraker rejected the request. Check the printer console.")
            return data["result"]
        except (requests.RequestException, ValueError) as exc:
            raise ConnectionError("Connection failed or timed out; verify address, network and printer state.") from None

    def test(self):
        data = self.request("/server/info")
        if "klippy_connected" not in data:
            raise ConnectionError("The address did not return Moonraker server information.")
        return "Connected" if data["klippy_connected"] else "Moonraker found · Klipper offline"

    def status(self):
        return self.request("/printer/objects/query?toolhead&print_stats&webhooks&pause_resume&virtual_sdcard&gcode_move")["status"]

    def gcode(self, script):
        return self.request("/printer/gcode/script", {"script": script})

    def close(self):
        self.session.close()


def probe(url):
    try:
        with requests.Session() as session:
            session.trust_env = False
            r = session.get(url + "/server/info", timeout=(0.35, 0.7), allow_redirects=False)
            if r.status_code == 200 and "klippy_connected" in r.json().get("result", {}):
                return (url, "Moonraker")
            if r.status_code in (401, 403) and isinstance(r.json().get("error"), dict):
                # Authentication can hide the server identity; the user tests before saving.
                return (url, "Protected service · test with API key")
    except (requests.RequestException, ValueError):
        pass
    return None


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
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    found = {}

    class Listener(ServiceListener):
        def add_service(self, zc, kind, name):
            info = zc.get_service_info(kind, name, timeout=800)
            if info:
                for address in info.parsed_addresses():
                    if ":" not in address:
                        found[endpoint(f"http://{address}:{info.port}")] = name.removesuffix("._moonraker._tcp.local.")

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
            for result in pool.map(probe, urls):
                if result:
                    found.setdefault(*result)
    return sorted(found.items(), key=lambda item: item[1].lower())

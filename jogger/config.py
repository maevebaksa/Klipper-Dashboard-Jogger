"""Private configuration and generation of the upstream KlipperScreen config."""
import configparser
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_GAMEPAD = {
    "guid": "", "axes": {"x": 0, "y": 1, "z": 3},
    "invert": {"x": False, "y": True, "z": True},
    "deadzone": 0.22, "enable_button": None, "buttons": {},
}


def endpoint(url):
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    u = urlsplit(url)
    if (u.scheme not in ("http", "https") or not u.hostname or u.username or
            u.password or u.query or u.fragment or any(c.isspace() for c in url)):
        raise ValueError("Enter a plain HTTP(S) Moonraker or custom app URL, without login/query parameters.")
    port = u.port or (443 if u.scheme == "https" else 80)
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    path = u.path.rstrip("/")
    if path.endswith("/websocket"):
        path = path[:-10]
    return f"{u.scheme}://{host}:{port}{path}"


def profile(name, url, api_key="", remote=False):
    name = name.strip()
    if not re.fullmatch(r"[\w .()-]{1,48}", name):
        raise ValueError("Use 1–48 letters, numbers, spaces, dots, parentheses or hyphens for the name.")
    if any(c in api_key for c in "\r\n"):
        raise ValueError("API keys cannot contain newlines.")
    url = endpoint(url)
    host = urlsplit(url).hostname.lower()
    oe = host == "octoeverywhere.com" or host.endswith(".octoeverywhere.com")
    if oe and not url.startswith("https:"):
        raise ValueError("OctoEverywhere requires HTTPS.")
    return {"name": name, "url": url, "api_key": api_key.strip(), "remote": bool(remote or oe)}


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("KDJ_CONFIG_DIR", "~/.config/klipper-dashboard-jogger")).expanduser()
        self.path = self.directory / "profiles.json"
        self.data = {"printers": [], "gamepad": json.loads(json.dumps(DEFAULT_GAMEPAD))}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    @property
    def printers(self):
        return self.data["printers"]

    def save(self):
        private_write(self.path, json.dumps(self.data, indent=2) + "\n")

    def put(self, item):
        index = next((i for i, p in enumerate(self.printers) if p["name"] == item["name"]), None)
        if index is None:
            self.printers.append(item)
        else:
            self.printers[index] = item
        self.save()

    def generate(self):
        from io import StringIO
        cfg = configparser.ConfigParser(interpolation=None)
        cfg["main"] = {"theme": "z-bolt", "font_size": "medium", "show_cursor": "False",
                       "screen_blanking": "off", "use_dpms": "False", "use_default_menu": "True"}
        for p in self.printers:
            u = urlsplit(p["url"])
            cfg["printer " + p["name"]] = {
                "moonraker_host": f"[{u.hostname}]" if ":" in u.hostname else u.hostname,
                "moonraker_port": str(u.port), "moonraker_ssl": str(u.scheme == "https"),
                "moonraker_path": u.path.strip("/"), "moonraker_api_key": p["api_key"],
            }
        out = StringIO()
        # Upstream uses ConfigParser's basic interpolation, including for URLs/keys.
        for section in cfg.sections():
            for key, value in list(cfg[section].items()):
                cfg[section][key] = value.replace("%", "%%")
        cfg.write(out)
        path = self.directory / "generated.conf"
        private_write(path, out.getvalue())
        return path

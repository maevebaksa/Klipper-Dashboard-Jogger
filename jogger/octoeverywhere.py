"""OctoEverywhere remote access helpers.

Two documented ways give an app a remote Moonraker URL:

* Shared Connections (https://octoeverywhere.com/sharedconnections): the user
  creates a unique URL on the OctoEverywhere website. No App ID is needed.
* App Connections (https://docs.octoeverywhere.com/app-connections/portal/):
  a hosted portal returns a URL plus header credentials. This needs an App ID
  assigned by OctoEverywhere.

Both URLs act like the printer's local Moonraker address. We only store the
returned per-printer values; no OctoEverywhere account password is used.
"""
import base64
import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit


PORTAL_URL = "https://octoeverywhere.com/appportal/v1/"
SHARED_CONNECTIONS_URL = "https://octoeverywhere.com/sharedconnections"

# Subdomains of octoeverywhere.com that are websites, not printer relays.
_SITE_HOSTS = {"octoeverywhere.com", "www.octoeverywhere.com", "help.octoeverywhere.com",
               "docs.octoeverywhere.com", "blog.octoeverywhere.com"}

# Shared 6xx codes from https://docs.octoeverywhere.com/error-codes/ and the
# App Connection Info API. They never collide with Moonraker's own statuses.
ERROR_CODES = {
    600: "OctoEverywhere had a temporary problem. Try again shortly.",
    601: "The printer is not connected to OctoEverywhere right now.",
    602: "OctoEverywhere timed out reaching the printer.",
    603: "This OctoEverywhere link no longer exists. Link remote access again.",
    604: "This OctoEverywhere link was revoked or expired. Link remote access again.",
    605: "OctoEverywhere remote access for apps needs Supporter Perks on the printer owner's account.",
}


class OctoEverywhereError(ValueError):
    pass


def error_message(status_code):
    """User-facing text for an OctoEverywhere 6xx status, or '' for other codes."""
    if 600 <= status_code < 700:
        return ERROR_CODES.get(status_code, f"OctoEverywhere error {status_code}.")
    return ""


def is_relay_host(host):
    host = (host or "").lower().rstrip(".")
    return host.endswith(".octoeverywhere.com") and host not in _SITE_HOSTS


def portal_url(app_id, printer_id="", return_url=""):
    app_id = (app_id or "").strip()
    if not app_id:
        raise OctoEverywhereError(
            "OctoEverywhere App ID is not configured for KlipperController."
        )
    params = {"appId": app_id}
    if printer_id:
        params["printerId"] = printer_id
    if return_url:
        params["returnUrl"] = return_url
    return PORTAL_URL + "?" + urlencode(params)


def parse_completion(url):
    """Parse the one-time App Connection completion URL returned by the portal."""
    try:
        query = parse_qs(urlsplit(url.strip()).query, keep_blank_values=True)
    except ValueError as exc:
        raise OctoEverywhereError("Invalid OctoEverywhere completion URL.") from exc

    def one(name, required=False):
        value = (query.get(name) or [""])[0].strip()
        if required and not value:
            raise OctoEverywhereError(
                f"OctoEverywhere completion URL is missing {name}."
            )
        return value

    if one("success").lower() not in ("1", "true", "yes"):
        raise OctoEverywhereError("OctoEverywhere setup did not complete successfully.")

    remote_url = one("url", True).rstrip("/")
    parsed = urlsplit(remote_url)
    if parsed.scheme != "https" or not is_relay_host(parsed.hostname):
        raise OctoEverywhereError("The returned App Connection URL is not an OctoEverywhere HTTPS URL.")

    bearer = one("authBearerToken")
    basic_user = one("authBasicHttpUser") or one("authbasichttpuser")
    basic_password = one("authBasicHttpPassword") or one("authbasichttppassword")
    if bearer:
        auth = {"type": "bearer", "token": bearer}
    elif basic_user and basic_password:
        auth = {"type": "basic", "username": basic_user, "password": basic_password}
    else:
        raise OctoEverywhereError("OctoEverywhere did not return App Connection authentication.")

    return {
        "kind": "app",
        "url": remote_url,
        "connection_id": one("id", True),
        "app_api_token": one("appApiToken", True),
        "printer_name": one("printername"),
        "user_url": one("userPrinterAccessUrl"),
        "last_local_ip": one("printerLastKnownLocalIp"),
        "auth": auth,
    }


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def parse_shared_connection(text, username="", password=""):
    """Validate a pasted Shared Connection URL.

    Accepts surrounding text (a copied message, for example) and takes the first
    OctoEverywhere relay URL in it. Credentials may be embedded as
    https://user:pass@host or given separately; either way they become an
    Authorization header and never stay in the stored URL.
    """
    text = (text or "").strip()
    candidates = _URL_IN_TEXT.findall(text) or ([text] if text else [])
    for raw in candidates:
        raw = raw.rstrip(".,;)")
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname
            embedded_user = unquote(parsed.username or "")
            embedded_password = unquote(parsed.password or "")
            port = parsed.port
        except ValueError:
            continue
        if not is_relay_host(host):
            continue
        if parsed.scheme.lower() != "https":
            raise OctoEverywhereError("OctoEverywhere links must start with https://")
        if parsed.query or parsed.fragment:
            raise OctoEverywhereError(
                "Paste the plain Shared Connection URL, without anything after ? or #."
            )
        netloc = host.lower() if port in (None, 443) else f"{host.lower()}:{port}"
        path = parsed.path.rstrip("/")
        if path.endswith("/websocket"):
            path = path[: -len("/websocket")]
        user = (username or "").strip() or embedded_user
        secret = password if password else embedded_password
        if any(c in user + secret for c in "\r\n"):
            raise OctoEverywhereError("Credentials cannot contain line breaks.")
        auth = {"type": "basic", "username": user, "password": secret} if user else {}
        return {
            "kind": "shared",
            "url": urlunsplit(("https", netloc, path, "", "")),
            "auth": auth,
        }
    if "octoeverywhere.com" in text.lower():
        raise OctoEverywhereError(
            "That is an OctoEverywhere website page, not a Shared Connection URL. "
            "Copy the URL shown for the shared connection itself."
        )
    raise OctoEverywhereError("Paste an OctoEverywhere Shared Connection URL.")


def authorization_header(profile):
    oe = profile.get("octoeverywhere") or {}
    auth = oe.get("auth") or {}
    if auth.get("type") == "bearer" and auth.get("token"):
        return "Bearer " + auth["token"]
    if auth.get("type") == "basic" and auth.get("username"):
        raw = f'{auth.get("username", "")}:{auth.get("password", "")}'.encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return ""


def describe(oe):
    """Short label for the linked remote access type."""
    if not oe:
        return ""
    return "Shared Connection" if oe.get("kind") == "shared" else "App Connection"

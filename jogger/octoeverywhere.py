"""OctoEverywhere App Connection helpers.

App Connections require a user-authorized portal flow.  We only store the
returned per-printer credentials; no OctoEverywhere account password is used.
"""
import base64
from urllib.parse import parse_qs, urlencode, urlsplit


PORTAL_URL = "https://octoeverywhere.com/appportal/v1/"


class OctoEverywhereError(ValueError):
    pass


def portal_url(app_id, printer_id=""):
    app_id = (app_id or "").strip()
    if not app_id:
        raise OctoEverywhereError(
            "OctoEverywhere App ID is not configured for KlipperController."
        )
    params = {"appId": app_id}
    if printer_id:
        params["printerId"] = printer_id
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
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "octoeverywhere.com" or host.endswith(".octoeverywhere.com")
    ):
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
        "url": remote_url,
        "connection_id": one("id", True),
        "app_api_token": one("appApiToken", True),
        "printer_name": one("printername"),
        "user_url": one("userPrinterAccessUrl"),
        "last_local_ip": one("printerLastKnownLocalIp"),
        "auth": auth,
    }


def authorization_header(profile):
    oe = profile.get("octoeverywhere") or {}
    auth = oe.get("auth") or {}
    if auth.get("type") == "bearer" and auth.get("token"):
        return "Bearer " + auth["token"]
    if auth.get("type") == "basic" and auth.get("username") is not None:
        raw = f'{auth.get("username", "")}:{auth.get("password", "")}'.encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return ""

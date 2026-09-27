import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from jogger.octoeverywhere import (
    OctoEverywhereError,
    authorization_header,
    error_message,
    parse_completion,
    parse_shared_connection,
    portal_url,
)


def test_shared_connection_plain_url():
    oe = parse_shared_connection("  https://Shared-ABC123.octoeverywhere.com/  ")
    assert oe == {"kind": "shared", "url": "https://shared-abc123.octoeverywhere.com", "auth": {}}
    assert authorization_header({"octoeverywhere": oe}) == ""


def test_shared_connection_found_inside_copied_text():
    oe = parse_shared_connection(
        "Here is my printer: https://shared-abc.octoeverywhere.com/websocket. Enjoy!"
    )
    assert oe["url"] == "https://shared-abc.octoeverywhere.com"


def test_shared_connection_embedded_credentials_move_to_header():
    oe = parse_shared_connection("https://us%40er:p%3Ass@shared-abc.octoeverywhere.com")
    assert oe["url"] == "https://shared-abc.octoeverywhere.com"
    assert "@" not in oe["url"]
    expected = "Basic " + base64.b64encode(b"us@er:p:ss").decode()
    assert authorization_header({"octoeverywhere": oe}) == expected


def test_shared_connection_separate_credentials_win():
    oe = parse_shared_connection("https://a:b@shared-abc.octoeverywhere.com", "user", "pass")
    assert oe["auth"] == {"type": "basic", "username": "user", "password": "pass"}


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "Paste"),
        ("https://octoeverywhere.com/sharedconnections", "website page"),
        ("https://www.octoeverywhere.com/", "website page"),
        ("http://shared-abc.octoeverywhere.com", "https"),
        ("https://shared-abc.octoeverywhere.com/?token=1", "without anything after"),
        ("https://shared-abc.octoeverywhere.com.evil.example", "website page"),
        ("https://evil.example/shared-abc.octoeverywhere.com", "website page"),
    ],
)
def test_shared_connection_rejects_non_relay_urls(text, message):
    with pytest.raises(OctoEverywhereError, match=message):
        parse_shared_connection(text)


def test_error_messages_cover_documented_codes():
    assert "Supporter Perks" in error_message(605)
    assert "revoked" in error_message(604)
    assert error_message(404) == ""
    assert error_message(699) == "OctoEverywhere error 699."


def test_portal_url_can_return_to_the_controller():
    url = portal_url("app", "", "http://192.168.1.9:1234/token/complete")
    query = parse_qs(urlsplit(url).query)
    assert query["returnUrl"] == ["http://192.168.1.9:1234/token/complete"]
    assert "printerId" not in query


def test_portal_url_preselects_local_printer():
    url = portal_url("klipper-controller", "printer-123")
    query = parse_qs(urlsplit(url).query)
    assert query["appId"] == ["klipper-controller"]
    assert query["printerId"] == ["printer-123"]


def test_parse_bearer_completion_and_header():
    result = parse_completion(
        "https://octoeverywhere.com/appportal/v1/complete?"
        "success=true&id=conn-1&"
        "url=https%3A%2F%2Fapp-example.octoeverywhere.com&"
        "appApiToken=app-token&authBearerToken=bearer-secret&"
        "printername=Voron&printerLastKnownLocalIp=192.168.1.20"
    )
    assert result["url"] == "https://app-example.octoeverywhere.com"
    assert result["connection_id"] == "conn-1"
    assert result["last_local_ip"] == "192.168.1.20"
    profile = {"octoeverywhere": result}
    assert authorization_header(profile) == "Bearer bearer-secret"


def test_parse_basic_completion_and_header():
    result = parse_completion(
        "https://octoeverywhere.com/appportal/v1/complete?"
        "success=1&id=conn-2&"
        "url=https%3A%2F%2Fapp-example.octoeverywhere.com&"
        "appApiToken=app-token&authBasicHttpUser=user&authBasicHttpPassword=pass"
    )
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    assert authorization_header({"octoeverywhere": result}) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://octoeverywhere.com/appportal/v1/complete?success=false",
        "https://octoeverywhere.com/appportal/v1/complete?success=true&id=x&"
        "url=http%3A%2F%2Fapp-example.octoeverywhere.com&appApiToken=x&authBearerToken=y",
        "https://octoeverywhere.com/appportal/v1/complete?success=true&id=x&"
        "url=https%3A%2F%2Fevil.example&appApiToken=x&authBearerToken=y",
    ],
)
def test_reject_invalid_completion(url):
    with pytest.raises(OctoEverywhereError):
        parse_completion(url)

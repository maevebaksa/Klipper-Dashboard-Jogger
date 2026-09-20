import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from jogger.octoeverywhere import (
    OctoEverywhereError,
    authorization_header,
    parse_completion,
    portal_url,
)


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

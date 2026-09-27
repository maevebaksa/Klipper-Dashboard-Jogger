import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from jogger import handoff as handoff_module
from jogger.handoff import Handoff, _private_client
from jogger.octoeverywhere import parse_completion, portal_url


def start(mode="shared", on_submit=None, **kwargs):
    seen = []

    def submit(fields):
        seen.append(fields)
        return on_submit(fields) if on_submit else (True, "Linked")
    h = Handoff(mode, submit, "Voron", host="127.0.0.1", bind="127.0.0.1", **kwargs)
    return h, h.start(), seen


def test_shared_form_submits_once_and_hides_the_path():
    h, url, seen = start()
    try:
        page = requests.get(url, timeout=3)
        assert page.status_code == 200
        assert "Shared Connection URL" in page.text and "Voron" in page.text
        assert page.headers["Referrer-Policy"] == "no-referrer"
        assert page.headers["Cache-Control"] == "no-store"

        done = requests.post(url, data={"url": "https://shared-a.octoeverywhere.com",
                                        "username": "", "password": ""}, timeout=3)
        assert "Linked" in done.text
        assert seen == [{"url": "https://shared-a.octoeverywhere.com", "username": "", "password": ""}]
        # After success the page only reports that it is done.
        again = requests.post(url, data={"url": "x"}, timeout=3)
        assert "Already linked" in again.text and len(seen) == 1
    finally:
        h.stop()


def test_failed_submission_keeps_the_form_and_value():
    h, url, _seen = start(on_submit=lambda f: (False, "That link reaches a different printer."))
    try:
        page = requests.post(url, data={"url": "https://shared-a.octoeverywhere.com"}, timeout=3)
        assert "different printer" in page.text
        assert "https://shared-a.octoeverywhere.com</textarea>" in page.text
        assert not h.done
    finally:
        h.stop()


def test_wrong_path_expiry_and_size_limits():
    h, url, seen = start(ttl=0.3)
    try:
        base = url.rsplit("/", 1)[0]
        assert requests.get(base + "/guess", timeout=3).status_code == 404
        big = requests.post(url, data={"url": "x" * (handoff_module.MAX_BODY_BYTES + 10)}, timeout=3)
        assert big.status_code == 413
        time.sleep(0.4)
        assert requests.get(url, timeout=3).status_code == 410
        assert seen == []
    finally:
        h.stop()


def test_too_many_bad_requests_stops_the_server(monkeypatch):
    monkeypatch.setattr(handoff_module, "MAX_BAD_REQUESTS", 3)
    h, url, _seen = start()
    base = url.rsplit("/", 1)[0]
    for _ in range(3):
        requests.get(base + "/nope", timeout=3)
    for _ in range(20):
        if h._server is None:
            break
        time.sleep(0.05)
    assert h._server is None


def test_app_mode_redirects_to_portal_and_captures_completion():
    captured = []

    def submit(fields):
        captured.append(parse_completion(fields["completion_url"]))
        return True, "Linked"
    h = Handoff("app", submit, "Voron", host="127.0.0.1", bind="127.0.0.1",
                portal_url=lambda ret: portal_url("devtest", "PID", ret))
    url = h.start()
    try:
        first = requests.get(url, allow_redirects=False, timeout=3)
        assert first.status_code == 302
        query = parse_qs(urlsplit(first.headers["Location"]).query)
        assert query["returnUrl"] == [url + "/complete"]
        assert query["printerId"] == ["PID"]

        done = requests.get(url + "/complete", params={
            "success": "true", "id": "c1", "url": "https://app-x.octoeverywhere.com",
            "appApiToken": "tok", "authBearerToken": "bear",
        }, timeout=3)
        assert "Linked" in done.text
        assert captured[0]["auth"] == {"type": "bearer", "token": "bear"}
    finally:
        h.stop()


@pytest.mark.parametrize("address, allowed", [
    ("192.168.1.5", True), ("10.0.0.2", True), ("127.0.0.1", True),
    ("fe80::1", True), ("::ffff:192.168.1.5", True),
    ("8.8.8.8", False), ("2001:4860::8888", False), ("nonsense", False),
])
def test_only_private_clients(address, allowed):
    assert _private_client(address) is allowed

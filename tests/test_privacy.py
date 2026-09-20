import logging
from jogger.privacy import redact_logs


def test_remote_capability_and_key_redacted():
    old = logging.getLogRecordFactory()
    try:
        redact_logs([{"url": "https://secret.octoeverywhere.com/capability", "api_key": "abc-key", "remote": True}])
        record = logging.getLogRecordFactory()("test", 20, "", 0, "url %s key %s", ("https://secret.octoeverywhere.com/capability", "abc-key"), None)
        assert "secret.octoeverywhere.com" not in record.getMessage()
        assert "abc-key" not in record.getMessage()
        assert "capability" not in record.getMessage()
    finally:
        logging.setLogRecordFactory(old)

"""Keep API keys and remote connection capabilities out of upstream logs."""
import logging
from urllib.parse import urlsplit


def redact_logs(printers):
    secrets = set()
    for printer in printers:
        if printer.get("api_key"):
            secrets.add(printer["api_key"])
        if printer.get("remote"):
            parsed = urlsplit(printer["url"])
            secrets.add(printer["url"])
            secrets.add(parsed.hostname)
            if parsed.path.strip("/"):
                secrets.add(parsed.path.strip("/"))
        oe = printer.get("octoeverywhere") or {}
        if oe.get("url"):
            parsed = urlsplit(oe["url"])
            secrets.add(oe["url"])
            secrets.add(parsed.hostname)
            if parsed.path.strip("/"):
                secrets.add(parsed.path.strip("/"))
        if oe.get("app_api_token"):
            secrets.add(oe["app_api_token"])
        auth = oe.get("auth") or {}
        for key in ("token", "username", "password"):
            if auth.get(key):
                secrets.add(auth[key])
    previous = logging.getLogRecordFactory()
    secrets.update(s.replace("%", "%%") for s in list(secrets))

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        message = record.getMessage()
        for secret in sorted(secrets, key=len, reverse=True):
            message = message.replace(secret, "[redacted]")
        record.msg, record.args = message, ()
        return record
    logging.setLogRecordFactory(factory)

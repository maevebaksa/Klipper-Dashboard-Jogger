"""The command the SSH panel runs in its embedded terminal. No GTK here.

The terminal asks for the host (defaulting to the selected printer), then the
user name, and hands over to ssh, which asks for the password itself. Values
are passed as arguments, never pasted into shell code.
"""
import ipaddress
from urllib.parse import urlsplit

# Shown in the terminal; "$1" is the default host, never interpolated as code.
PROMPT_SCRIPT = r'''
default_host=$1
printf '\033[1mSSH\033[0m  Press Ctrl+D or type exit to end the session.\n\n'
if [ -n "$default_host" ]; then
    read -r -p "Host [$default_host]: " host
    host=${host:-$default_host}
else
    read -r -p "Host: " host
fi
case "$host" in ""|-*|*[!A-Za-z0-9.:_%-]*) echo "Not a host name or address: $host"; exit 2;; esac
read -r -p "Username: " user
case "$user" in ""|-*|*[!A-Za-z0-9._-]*) echo "Not a user name: $user"; exit 2;; esac
exec ssh -o ConnectTimeout=10 -l "$user" -- "$host"
'''


def host_for(printer):
    """SSH host for a printer's LAN address, or '' when it has none."""
    if not printer:
        return ""
    for url in (printer.get("url"), printer.get("lan_fallback_url")):
        host = urlsplit(url or "").hostname or ""
        if not host or printer.get("remote") and url == printer.get("url"):
            continue
        try:
            ip = ipaddress.ip_address(host)
            if not (ip.is_private or ip.is_link_local):
                continue
        except ValueError:
            if not (host.endswith(".local") or "." not in host or host.endswith(".lan")):
                continue  # public names are not the printer's own machine
        return host
    return ""


def ssh_argv(default_host=""):
    """argv for the terminal: bash runs the prompt script with the default host as $1."""
    return ["/bin/bash", "-c", PROMPT_SCRIPT, "kdj-ssh", default_host]

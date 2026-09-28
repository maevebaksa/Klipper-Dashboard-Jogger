"""Notice a Moonraker connection that stopped making progress. No GTK here.

KlipperScreen initializes a printer through a chain of websocket requests
(server info, printer info, config, object list, object query), and none of
them has a timeout of its own. If one reply is lost, for example to a Wi-Fi
drop or an OctoEverywhere relay hiccup that leaves the TCP connection
half-open, the splash screen waits on "Initializing Klipper Connection"
forever. Two defenses:

* A websocket keepalive, so a dead connection is closed (and KlipperScreen's
  normal reconnect or our LAN/OctoEverywhere failover runs) instead of
  lingering.
* A progress watchdog while a printer is connecting: any received message
  counts as progress; no progress for too long restarts the connection.
"""

# A healthy LAN connection opens and initializes in a second or two. The limit
# must exceed the slowest healthy gap between messages: KlipperScreen's own
# "Klipper is starting" loop polls every 5 s, and the full object query over
# OctoEverywhere can take several seconds.
LOCAL_STALL_S = 15
REMOTE_STALL_S = 30
# Restarts per user-initiated connection before giving up and saying so.
MAX_STALL_RESTARTS = 3
# Moonraker (Tornado) answers websocket pings itself. websocket-client requires
# the timeout to be below the interval.
PING_INTERVAL_S = 20
PING_TIMEOUT_S = 10


def stalled(now, last_progress, remote):
    """True when a connecting printer has been silent for too long."""
    return now - last_progress > (REMOTE_STALL_S if remote else LOCAL_STALL_S)


def run_forever(ws, ping_interval=PING_INTERVAL_S, ping_timeout=PING_TIMEOUT_S):
    """Run a websocket-client WebSocketApp with a keepalive."""
    return ws.run_forever(ping_interval=ping_interval, ping_timeout=ping_timeout)

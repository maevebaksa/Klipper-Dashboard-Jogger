import base64
import hashlib
import socket
import threading
import time

import pytest

from jogger import watchdog

websocket = pytest.importorskip("websocket")


def test_stall_limits():
    assert not watchdog.stalled(100.0, 90.0, remote=False)
    assert watchdog.stalled(100.0, 100.0 - watchdog.LOCAL_STALL_S - 1, remote=False)
    assert not watchdog.stalled(100.0, 100.0 - watchdog.LOCAL_STALL_S - 1, remote=True)
    assert watchdog.stalled(100.0, 100.0 - watchdog.REMOTE_STALL_S - 1, remote=True)
    assert watchdog.PING_TIMEOUT_S < watchdog.PING_INTERVAL_S


@pytest.fixture
def silent_server():
    """Accepts the websocket handshake, then never sends anything again,
    like a half-open connection after a network drop."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []

    def serve():
        conn, _ = listener.accept()
        held.append(conn)
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(1024)
        key = [line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
               if line.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        # Read and ignore everything (pings included); never reply.
        try:
            while conn.recv(1024):
                pass
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    yield f"ws://127.0.0.1:{listener.getsockname()[1]}/websocket"
    for conn in held:
        conn.close()
    listener.close()


def test_keepalive_closes_a_silent_connection(silent_server):
    opened, closed = threading.Event(), threading.Event()
    ws = websocket.WebSocketApp(silent_server, on_open=lambda *a: opened.set(),
                                on_close=lambda *a: closed.set())
    started = time.monotonic()
    thread = threading.Thread(target=watchdog.run_forever, args=(ws, 1, 0.5), daemon=True)
    thread.start()
    assert opened.wait(5)
    # Without the keepalive this would wait forever.
    assert closed.wait(10), "silent connection was never closed"
    assert time.monotonic() - started < 10

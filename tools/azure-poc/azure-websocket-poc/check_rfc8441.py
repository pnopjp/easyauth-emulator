"""
Client-side check used to verify real Azure App Service's WebSocket-over-HTTP/2
(RFC 8441) behavior against app.py deployed there (see README.md).

Talks raw HTTP/2 directly (not via a browser) so the result doesn't depend on
any particular browser's RFC 8441 support or quirks: opens an h2 connection,
checks whether the server advertises SETTINGS_ENABLE_CONNECT_PROTOCOL, and if
so, attempts an RFC 8441 extended CONNECT to /ws/echo and sends one WebSocket
text frame through the tunnel to confirm the echo round-trips.

Usage:
    python check_rfc8441.py <app-name>.azurewebsites.net
"""

import os
import socket
import ssl
import sys

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RemoteSettingsChanged,
    ResponseReceived,
    StreamEnded,
    StreamReset,
)
from h2.settings import SettingCodes

PORT = 443


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: python {sys.argv[0]} <host>", file=sys.stderr)
        sys.exit(1)
    host = sys.argv[1]

    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2"])
    raw_sock = socket.create_connection((host, PORT), timeout=15)
    sock = ctx.wrap_socket(raw_sock, server_hostname=host)
    print(f"TLS ALPN negotiated: {sock.selected_alpn_protocol()}")

    conn = H2Connection(config=H2Configuration(client_side=True))
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())

    server_settings_seen = False
    while not server_settings_seen:
        data = sock.recv(65535)
        if not data:
            break
        events = conn.receive_data(data)
        for event in events:
            if isinstance(event, RemoteSettingsChanged):
                server_settings_seen = True
        outbound = conn.data_to_send()
        if outbound:
            sock.sendall(outbound)

    current_value = conn.remote_settings.get(SettingCodes.ENABLE_CONNECT_PROTOCOL, 0)
    print(f"Server-advertised SETTINGS_ENABLE_CONNECT_PROTOCOL = {current_value}")
    if not current_value:
        print("=> Server does not advertise ENABLE_CONNECT_PROTOCOL; a compliant "
              "client must not attempt RFC 8441 here.")
        return

    print("=> Attempting an RFC 8441 extended CONNECT to /ws/echo ...")
    stream_id = conn.get_next_available_stream_id()
    headers = [
        (":method", "CONNECT"),
        (":protocol", "websocket"),
        (":scheme", "https"),
        (":authority", host),
        (":path", "/ws/echo"),
        ("sec-websocket-version", "13"),
        ("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ=="),
        ("origin", f"https://{host}"),
    ]
    conn.send_headers(stream_id, headers)
    sock.sendall(conn.data_to_send())

    sock.settimeout(30)
    status = None
    body = b""
    sent_ws_frame = False
    for i in range(20):
        try:
            data = sock.recv(65535)
        except TimeoutError:
            print(f"[{i}] recv timed out (30s) waiting for more data")
            break
        if not data:
            print(f"[{i}] connection closed by peer")
            break
        events = conn.receive_data(data)
        outbound = conn.data_to_send()
        if outbound:
            sock.sendall(outbound)
        for event in events:
            print(f"[{i}] event: {type(event).__name__} -> {event}")
            if isinstance(event, ResponseReceived):
                status = dict(event.headers).get(b":status")
            elif isinstance(event, DataReceived):
                body += event.data
                if len(body) >= 2:
                    b1, b2 = body[0], body[1]
                    opcode = b1 & 0x0F
                    length = b2 & 0x7F
                    payload = body[2:2 + length]
                    print(f"Echoed WS frame: opcode=0x{opcode:x} payload={payload!r}")
                    return
            elif isinstance(event, StreamReset):
                print(f"Stream RESET by server. error_code={event.error_code}")
                return
            elif isinstance(event, StreamEnded):
                print(f"Extended CONNECT response status: {status}, body: {body[:300]!r}")
                return
            elif isinstance(event, ConnectionTerminated):
                print(f"Connection terminated: error_code={event.error_code} "
                      f"additional_data={event.additional_data}")
                return

        if status == b"200" and not sent_ws_frame:
            sent_ws_frame = True
            payload = b"hello"
            mask_key = os.urandom(4)
            masked = bytes(b ^ mask_key[j % 4] for j, b in enumerate(payload))
            ws_frame = bytes([0x81, 0x80 | len(payload)]) + mask_key + masked
            print(f"[{i}] tunnel established (status 200); sending WS text frame 'hello'")
            conn.send_data(stream_id, ws_frame)
            sock.sendall(conn.data_to_send())
        elif status is not None and status != b"200":
            print(f"Extended CONNECT failed with status {status}, body: {body[:300]!r}")
            return

    print(f"No terminal response after several reads. status={status} body={body[:300]!r}")


if __name__ == "__main__":
    main()

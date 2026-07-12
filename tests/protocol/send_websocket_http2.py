"""
Bootstrap a WebSocket over plaintext HTTP/2 (h2c) via RFC 8441's extended
CONNECT (:method CONNECT, :protocol websocket), send one text frame, and
print what comes back.

Useful for manually testing the gateway's RFC 8441 support end to end
without a browser (browsers require TLS + ALPN "h2" for HTTP/2 and won't
speak h2c at all, and even over TLS, whether a given browser attempts RFC
8441 for a given `new WebSocket(...)` call depends on implementation
details). This talks the wire protocol directly instead.

Usage:
    python -m tests.protocol.send_websocket_http2 <host> <port> <path> [--text "hello"]

Example (against the gateway, once configured with APP_UPSTREAM pointed at
tests/protocol/app.py's HTTP port, HTTP20_ENABLED=true, and SKIP_AUTH_ROUTES
covering the path so this doesn't need a real login session):
    python -m tests.protocol.send_websocket_http2 127.0.0.1 8080 /ws/echo
"""

import argparse
import base64
import socket

import h2.config
import h2.connection
import h2.events
import h2.settings


def send_websocket_http2(host: str, port: int, path: str, text: str) -> bytes:
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(10)
    sock.sendall(conn.data_to_send())

    # RFC 8441 section 3: a client must see the server advertise
    # ENABLE_CONNECT_PROTOCOL before attempting an extended CONNECT — h2
    # enforces this locally, so wait for that SETTINGS frame first.
    while not conn.remote_settings.get(h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL, 0):
        data = sock.recv(65536)
        if not data:
            raise ConnectionError("connection closed before advertising ENABLE_CONNECT_PROTOCOL — "
                                   "is HTTP20_ENABLED set on the gateway?")
        conn.receive_data(data)
        out = conn.data_to_send()
        if out:
            sock.sendall(out)
    print("server advertises SETTINGS_ENABLE_CONNECT_PROTOCOL = 1")

    stream_id = conn.get_next_available_stream_id()
    key = base64.b64encode(b"0123456789012345").decode()
    conn.send_headers(stream_id, [
        (":method", "CONNECT"),
        (":protocol", "websocket"),
        (":scheme", "http"),
        (":authority", f"{host}:{port}"),
        (":path", path),
        ("sec-websocket-version", "13"),
        ("sec-websocket-key", key),
    ])
    sock.sendall(conn.data_to_send())
    print(f"sent RFC 8441 extended CONNECT to {path}")

    payload = text.encode()
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    ws_frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked

    status = None
    sent_frame = False
    body = bytearray()
    while True:
        data = sock.recv(65536)
        if not data:
            break
        for event in conn.receive_data(data):
            if isinstance(event, h2.events.ResponseReceived):
                status = dict(event.headers).get(b":status", b"").decode()
                print(f"extended CONNECT response: :status = {status}")
                if status != "200":
                    return bytes(body)
            elif isinstance(event, h2.events.DataReceived):
                body += event.data
                conn.acknowledge_received_data(len(event.data), event.stream_id)
            elif isinstance(event, (h2.events.StreamEnded, h2.events.StreamReset, h2.events.ConnectionTerminated)):
                return bytes(body)
        out = conn.data_to_send()
        if out:
            sock.sendall(out)
        if status == "200" and not sent_frame:
            sent_frame = True
            print(f"tunnel established; sending WS text frame: {text!r}")
            conn.send_data(stream_id, ws_frame)
            sock.sendall(conn.data_to_send())
        if len(body) >= 2:
            break
    return bytes(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("path")
    parser.add_argument("--text", default="hello via RFC 8441")
    args = parser.parse_args()

    body = send_websocket_http2(args.host, args.port, args.path, args.text)
    if len(body) >= 2:
        opcode = body[0] & 0x0F
        length = body[1] & 0x7F
        payload = body[2:2 + length]
        print(f"echoed WS frame: opcode=0x{opcode:x} payload={payload!r}")
    else:
        print(f"no WS frame echoed back (raw bytes received: {bytes(body)!r})")


if __name__ == "__main__":
    main()

"""
Backend used to verify real Azure App Service's WebSocket-over-HTTP/2 (RFC 8441)
behavior (see README.md). Understands both plain HTTP/1.1 (including the classic
WebSocket Upgrade handshake) and h2c on the same port, detected by peeking at the
first bytes for the HTTP/2 client connection preface.

Advertises SETTINGS_ENABLE_CONNECT_PROTOCOL=1 on the h2c side so an RFC 8441
extended CONNECT is valid there too, though in practice Azure never uses that
path - see README.md for what was actually observed.
"""

import base64
import hashlib
import os
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RequestReceived,
    StreamEnded,
    StreamReset,
)
from h2.settings import SettingCodes

PORT = int(os.environ.get("PORT", "8000"))
_H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _handle_http11(sock: socket.socket) -> None:
    try:
        f = sock.makefile("rb")
        request_line = f.readline()
        headers = {}
        while True:
            line = f.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.strip().lower().decode()] = v.strip().decode()
        print(f"HTTP/1.1 request: {request_line!r} headers={headers}", flush=True)

        path = request_line.split(b" ")[1] if b" " in request_line else b""
        if path == b"/ws/echo" and headers.get("upgrade", "").lower() == "websocket":
            key = headers.get("sec-websocket-key", "")
            print("HTTP/1.1: performing WS 101 handshake", flush=True)
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {_ws_accept_key(key)}\r\n\r\n"
            ).encode()
            sock.sendall(resp)
            while True:
                header = f.read(2)
                if len(header) < 2:
                    break
                b1, b2 = header
                opcode = b1 & 0x0F
                masked = (b2 & 0x80) != 0
                length = b2 & 0x7F
                if length == 126:
                    length = int.from_bytes(f.read(2), "big")
                elif length == 127:
                    length = int.from_bytes(f.read(8), "big")
                mask_key = f.read(4) if masked else b""
                payload = f.read(length)
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                print(f"HTTP/1.1 WS frame received: opcode={opcode} payload={payload!r}", flush=True)
                if opcode == 0x8:
                    break
                if opcode in (0x1, 0x2):
                    out_payload = b"echo: " + payload
                    out_header = bytes([0x80 | opcode, len(out_payload)])
                    sock.sendall(out_header + out_payload)
            return

        body = b"websocket poc backend ok (HTTP/1.1 path, e.g. Azure warmup probe)\n"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        sock.sendall(response)
    except (ConnectionError, OSError) as exc:
        print(f"HTTP/1.1 handler error: {exc}", flush=True)
    finally:
        sock.close()


def _handle_h2c(sock: socket.socket) -> None:
    config = H2Configuration(client_side=False)
    conn = H2Connection(config=config)
    conn.initiate_connection()
    conn.update_settings({SettingCodes.ENABLE_CONNECT_PROTOCOL: 1})
    sock.sendall(conn.data_to_send())

    ws_streams = set()
    try:
        while True:
            data = sock.recv(65535)
            if not data:
                break
            events = conn.receive_data(data)
            for event in events:
                if isinstance(event, RequestReceived):
                    headers = dict(event.headers)
                    method = headers.get(b":method", b"").decode()
                    path = headers.get(b":path", b"").decode()
                    protocol = headers.get(b":protocol", b"").decode()
                    print(f"RequestReceived stream={event.stream_id} method={method} path={path} protocol={protocol!r}", flush=True)
                    if method == "CONNECT" and protocol == "websocket":
                        ws_streams.add(event.stream_id)
                        conn.send_headers(event.stream_id, [(":status", "200")])
                    else:
                        body = f"websocket poc backend ok (h2c path): method={method} path={path}\n".encode()
                        conn.send_headers(event.stream_id, [(":status", "200"), ("content-type", "text/plain")])
                        conn.send_data(event.stream_id, body, end_stream=True)
                elif isinstance(event, DataReceived):
                    conn.acknowledge_received_data(len(event.data), event.stream_id)
                    if event.stream_id in ws_streams:
                        print(f"DataReceived on ws stream={event.stream_id} bytes={event.data.hex()}", flush=True)
                        conn.send_data(event.stream_id, event.data)
                elif isinstance(event, StreamEnded):
                    print(f"StreamEnded stream={event.stream_id}", flush=True)
                elif isinstance(event, StreamReset):
                    print(f"StreamReset stream={event.stream_id}", flush=True)
                elif isinstance(event, ConnectionTerminated):
                    return
            outbound = conn.data_to_send()
            if outbound:
                sock.sendall(outbound)
    except (ConnectionError, OSError) as exc:
        print(f"connection error: {exc}", flush=True)
    finally:
        sock.close()


def main() -> None:
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("0.0.0.0", PORT))
    server_sock.listen(20)
    print(f"listening on 0.0.0.0:{PORT} (HTTP/1.1 and h2c, auto-detected)", flush=True)
    while True:
        client_sock, addr = server_sock.accept()
        try:
            preface = client_sock.recv(len(_H2_PREFACE), socket.MSG_PEEK)
        except OSError:
            client_sock.close()
            continue
        target = _handle_h2c if preface == _H2_PREFACE else _handle_http11
        threading.Thread(target=target, args=(client_sock,), daemon=True).start()


if __name__ == "__main__":
    main()

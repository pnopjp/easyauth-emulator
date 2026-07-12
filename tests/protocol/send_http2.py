"""
Send a single request over plaintext HTTP/2 (h2c) and print the response.

Useful for manually testing HTTP20_ENABLED end to end against ordinary
(non-gRPC) routes — for gRPC itself, use grpcurl or a real gRPC client
instead, since this only speaks generic HTTP/2, not gRPC framing. Windows
builds of curl typically lack HTTP/2 support entirely (`curl --version` has
no "HTTP2" in Features), so this fills that gap for local testing.

Usage:
    python -m tests.protocol.send_http2 <host> <port> <path> [--method GET] [--body TEXT] [--header "Name: value"]...

Examples:
    python -m tests.protocol.send_http2 localhost 8080 /healthz
    python -m tests.protocol.send_http2 localhost 8080 /.auth/login
    python -m tests.protocol.send_http2 localhost 8080 /some/api --method POST --body '{"a":1}' --header "Content-Type: application/json"
"""

import argparse
import socket

import h2.config
import h2.connection
import h2.events


def send_http2(host: str, port: int, method: str, path: str,
                headers: "list[tuple[str, str]]", body: "bytes | None") -> "tuple[str, list[tuple[str, str]], bytes]":
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(conn.data_to_send())

        stream_id = conn.get_next_available_stream_id()
        request_headers = [
            (":method", method),
            (":path", path),
            (":scheme", "http"),
            (":authority", f"{host}:{port}"),
        ] + headers
        conn.send_headers(stream_id, request_headers, end_stream=body is None)
        sock.sendall(conn.data_to_send())
        if body is not None:
            conn.send_data(stream_id, body, end_stream=True)
            sock.sendall(conn.data_to_send())

        status = ""
        resp_headers: "list[tuple[str, str]]" = []
        resp_body = bytearray()
        done = False
        sock.settimeout(10)
        while not done:
            data = sock.recv(65536)
            if not data:
                break
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    for name, value in event.headers:
                        name = name.decode() if isinstance(name, bytes) else name
                        value = value.decode() if isinstance(value, bytes) else value
                        if name == ":status":
                            status = value
                        else:
                            resp_headers.append((name, value))
                elif isinstance(event, h2.events.DataReceived):
                    resp_body += event.data
                    conn.acknowledge_received_data(len(event.data), event.stream_id)
                elif isinstance(event, (h2.events.StreamEnded, h2.events.ConnectionTerminated)):
                    done = True
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
        return status, resp_headers, bytes(resp_body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("path")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--body", default=None)
    parser.add_argument("--header", action="append", default=[], help='e.g. "Content-Type: application/json"')
    args = parser.parse_args()

    headers = []
    for item in args.header:
        name, _, value = item.partition(":")
        headers.append((name.strip().lower(), value.strip()))
    body = args.body.encode() if args.body is not None else None

    status, resp_headers, resp_body = send_http2(args.host, args.port, args.method, args.path, headers, body)
    print(f"status: {status}")
    for name, value in resp_headers:
        print(f"{name}: {value}")
    print()
    print(resp_body.decode(errors="replace"))


if __name__ == "__main__":
    main()

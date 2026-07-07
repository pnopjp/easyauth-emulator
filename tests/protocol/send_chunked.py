"""
Send a genuinely multi-chunk `Transfer-Encoding: chunked` POST request.

curl sends a known-length body as a single chunk (see tests/protocol/README.md);
this splits the body into several chunks and sends them with a delay between
each, closer to a real streaming client.

Usage:
    python -m tests.protocol.send_chunked <host> <port> <path> [--text ...] [--chunk-size 8] [--chunk-delay 0.2]

Example:
    python -m tests.protocol.send_chunked localhost 8082 /chunked/echo
    python -m tests.protocol.send_chunked localhost 8080 /chunked/echo
"""

import argparse
import socket
import time


def send_chunked(host: str, port: int, path: str, parts: "list[str]", delay: float) -> bytes:
    request_line = f"POST {path} HTTP/1.1\r\n"
    headers = (
        f"Host: {host}:{port}\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall((request_line + headers).encode())
        for part in parts:
            data = part.encode()
            sock.sendall(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            print(f"sent chunk ({len(data)} bytes): {part!r}")
            time.sleep(delay)
        sock.sendall(b"0\r\n\r\n")
        print("sent terminating chunk")
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("path")
    parser.add_argument("--text", default="chunked request body test")
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--chunk-delay", type=float, default=0.2)
    args = parser.parse_args()

    parts = [args.text[i:i + args.chunk_size] for i in range(0, len(args.text), args.chunk_size)]
    print(f"sending {len(parts)} chunks to {args.host}:{args.port}{args.path}")
    response = send_chunked(args.host, args.port, args.path, parts, args.chunk_delay)
    print("--- response ---")
    print(response.decode(errors="replace"))


if __name__ == "__main__":
    main()

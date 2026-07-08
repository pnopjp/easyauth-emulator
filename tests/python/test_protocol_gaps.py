"""
Regression tests for the protocol gaps tracked in ToDo.md: WebSocket,
SSE/streaming, chunked request bodies, and gRPC. Automates the manual
procedure in tests/protocol/README.md.

WebSocket/SSE/chunked-body tests assert the CORRECT (fully working) behavior
and are marked xfail(strict=True): they currently fail because the gap is
real, and will flip to an unexpected pass once the gap is closed — pytest
reports that as a failure (strict=True), which is the signal to remove the
xfail marker.

gRPC is implemented (as of the HTTP/2 support work) but is opt-in — off by
default, like App Service's own http20ProxyFlag. test_grpc_call_through_gateway
uses a separate gateway instance with HTTP20_ENABLED/HTTP20_PROXY_MODE=grpc-only
set, and asserts success directly (no xfail: this gap is closed, for that
configuration). test_grpc_disabled_by_default_does_not_hang confirms the
default (HTTP20_ENABLED unset) gateway used by the other tests still fails
fast rather than accidentally starting to accept gRPC.

Run:
    pip install -r requirements-test.txt
    pytest tests/python/test_protocol_gaps.py -v
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROTOCOL_DIR = REPO_ROOT / "tests" / "protocol"
sys.path.insert(0, str(PROTOCOL_DIR))

from send_chunked import send_chunked  # noqa: E402

try:
    import grpc  # noqa: E402
    import echo_pb2  # noqa: E402
    import echo_pb2_grpc  # noqa: E402
    HAS_GRPC = True
except ImportError:
    HAS_GRPC = False

GRPC_TIMEOUT = 5.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} did not start listening within {timeout}s")


def _stop(proc: "subprocess.Popen | None") -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _empty_config(tmp_path_factory) -> Path:
    # An explicit --config pointing at an empty file keeps this hermetic —
    # without it, src/app.py falls back to reading ./config.toml (a real,
    # secret-bearing dev config) when run with cwd=REPO_ROOT.
    path = tmp_path_factory.mktemp("protocol_gaps") / "empty.toml"
    path.write_text("")
    return path


def _start_gateway(tmp_path_factory, extra_env: dict) -> "tuple[subprocess.Popen, int]":
    gateway_port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "src/app.py", "--config", str(_empty_config(tmp_path_factory))],
        cwd=REPO_ROOT,
        env={**os.environ, "SITE_PORT": str(gateway_port), **extra_env},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    _wait_for_port("127.0.0.1", gateway_port)
    return proc, gateway_port


@pytest.fixture(scope="module")
def protocol_app(tmp_path_factory):
    """The verification app from tests/protocol/ — listens on its own HTTP
    port (WebSocket/SSE/chunked-body test endpoints) and its own gRPC port,
    independent of any gateway instance."""
    http_port = _free_port()
    grpc_port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.protocol.app"],
        cwd=REPO_ROOT,
        env={**os.environ, "PROTOCOL_APP_PORT": str(http_port), "PROTOCOL_APP_GRPC_PORT": str(grpc_port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_for_port("127.0.0.1", http_port)
        _wait_for_port("127.0.0.1", grpc_port)
    except Exception:
        _stop(proc)
        raise
    yield {"http_port": http_port, "grpc_port": grpc_port}
    _stop(proc)


@pytest.fixture(scope="module")
def gateway_port(tmp_path_factory, protocol_app):
    """Default gateway: HTTP/2 off, upstream is the verification app's HTTP
    port — used by the WebSocket/SSE/chunked-body tests, which are unrelated
    to HTTP/2 and unaffected by it being enabled or not."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['http_port']}",
        "SKIP_AUTH_ROUTES": "/ws/,/sse/,/chunked/,/echo\\.Echo/",
    })
    yield port
    _stop(proc)


@pytest.fixture(scope="module")
def grpc_gateway_port(tmp_path_factory, protocol_app):
    """A separate gateway instance with HTTP20_ENABLED and
    HTTP20_PROXY_MODE=grpc-only, upstream pointed at the verification app's
    gRPC port — APP_UPSTREAM is a single address, so gRPC (a real HTTP/2
    server) and the plain HTTP endpoints can't share one gateway instance."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['grpc_port']}",
        "SKIP_AUTH_ROUTES": "/echo\\.Echo/",
        "HTTP20_ENABLED": "true",
        "HTTP20_PROXY_MODE": "grpc-only",
    })
    yield port
    _stop(proc)


@pytest.mark.xfail(
    strict=True,
    reason="ToDo.md: WebSocket is not supported (HTTP/1.1 request/response proxying only)",
)
def test_websocket_upgrade_and_echo_through_gateway(gateway_port):
    port = gateway_port
    key = base64.b64encode(b"0123456789012345").decode()
    request = (
        "GET /ws/echo HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode())
        response = sock.recv(4096)
        assert response.startswith(b"HTTP/1.1 101"), (
            f"expected a 101 Switching Protocols upgrade, got: {response[:200]!r}"
        )

        payload = b"hello"
        mask = b"\x01\x02\x03\x04"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
        sock.sendall(frame)
        echoed = sock.recv(4096)
        assert echoed == bytes([0x81, len(payload)]) + payload


@pytest.mark.xfail(
    strict=True,
    reason="ToDo.md: SSE/streaming is not supported "
           "(_proxy_to buffers the full upstream response before replying)",
)
def test_sse_streamed_incrementally_through_gateway(gateway_port):
    port = gateway_port
    request = f"GET /sse/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode())
        sock.settimeout(3)
        try:
            first_bytes = sock.recv(4096)
        except socket.timeout:
            pytest.fail("no data arrived within 3s — the response is being buffered instead of streamed")
        assert first_bytes, "connection closed immediately with no data"


@pytest.mark.xfail(
    strict=True,
    reason="ToDo.md: chunked request bodies are dropped "
           "(_proxy_to only reads a body when Content-Length is present)",
)
def test_chunked_request_body_forwarded_through_gateway(gateway_port):
    port = gateway_port
    text = "chunked request body test"
    parts = [text[i:i + 8] for i in range(0, len(text), 8)]
    response = send_chunked("127.0.0.1", port, "/chunked/echo", parts, delay=0)
    body = response.split(b"\r\n\r\n", 1)[1]
    result = json.loads(body)
    assert result["received_bytes"] == len(text.encode())


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_grpc_disabled_by_default_does_not_hang(gateway_port):
    """HTTP20_ENABLED/HTTP20_PROXY_MODE default to off (mirrors App Service's
    http20ProxyFlag defaulting to disabled) — a gRPC call against a gateway
    that never opted in must fail promptly, not hang."""
    channel = grpc.insecure_channel(f"127.0.0.1:{gateway_port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        with pytest.raises(grpc.RpcError):
            stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
    finally:
        channel.close()


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_grpc_call_through_gateway(grpc_gateway_port):
    """With HTTP20_ENABLED and HTTP20_PROXY_MODE=grpc-only, a real gRPC call
    survives the gateway end to end (client → gateway → upstream gRPC
    service), including the grpc-status trailer."""
    channel = grpc.insecure_channel(f"127.0.0.1:{grpc_gateway_port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        response = stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
        assert "world" in response.message
    finally:
        channel.close()

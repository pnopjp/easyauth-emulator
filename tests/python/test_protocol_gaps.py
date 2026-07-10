"""
Regression tests for the protocol gaps tracked in ToDo.md: WebSocket,
SSE/streaming, chunked request bodies, and gRPC. Automates the manual
procedure in tests/protocol/README.md.

The WebSocket test asserts the CORRECT (fully working) behavior and is
marked xfail(strict=True): it currently fails because the gap is real, and
will flip to an unexpected pass once the gap is closed — pytest reports that
as a failure (strict=True), which is the signal to remove the xfail marker.
The chunked-request-body and SSE/streaming gaps have both been closed
(_read_request_body now decodes Transfer-Encoding: chunked; _proxy_to's
HTTP/1.1 relay now streams the upstream response incrementally via
_stream_response instead of buffering it), so those tests assert success
directly.

gRPC is implemented (as of the HTTP/2 support work) but is opt-in — off by
default, like App Service's own http20ProxyFlag. test_grpc_call_through_gateway
uses a separate gateway instance with HTTP20_ENABLED/HTTP20_PROXY_MODE=grpc-only
set, and asserts success directly (no xfail: this gap is closed, for that
configuration). test_grpc_disabled_by_default_does_not_hang confirms the
default (HTTP20_ENABLED unset) gateway used by the other tests still fails
fast rather than accidentally starting to accept gRPC.

APPSERVICE_HTTP20_ONLY_PORT (the App Service-style dedicated gRPC port) and
two bugs found while verifying it by hand are covered by their own fixtures
below:
- test_http20_disabled_forces_http1_upstream_even_when_proxy_mode_is_all:
  HTTP20_PROXY_MODE=all used to force an HTTP/2 upstream relay even when
  HTTP20_ENABLED was off (SITE_PORT never accepts HTTP/2 from a client in
  that case, so there was nothing to "preserve").
- test_unauthenticated_grpc_via_dedicated_port_fails_fast: an unauthenticated
  gRPC call used to hang until its own deadline, because the gateway replied
  with a browser-style redirect to /.auth/login that a gRPC client can't
  follow.
- test_appservice_dedicated_port_* / test_site_port_stops_detecting_grpc_*:
  the dedicated port itself, and its effect on SITE_PORT's own
  HTTP20_PROXY_MODE=grpc-only behavior once it's configured.

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
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROTOCOL_DIR = REPO_ROOT / "tests" / "protocol"
sys.path.insert(0, str(PROTOCOL_DIR))

from send_chunked import send_chunked  # noqa: E402
from send_http2 import send_http2  # noqa: E402

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


def _start_gateway_with_appservice_port(tmp_path_factory, extra_env: dict) -> "tuple[subprocess.Popen, int, int]":
    dedicated_port = _free_port()
    proc, gateway_port = _start_gateway(tmp_path_factory, {
        "APPSERVICE_HTTP20_ONLY_PORT": str(dedicated_port),
        **extra_env,
    })
    _wait_for_port("127.0.0.1", dedicated_port)
    return proc, gateway_port, dedicated_port


class _RawSniffer:
    """Tiny raw TCP responder used as APP_UPSTREAM to record the first bytes
    of each connection the gateway relays — enough to tell an HTTP/1.1
    request line apart from the HTTP/2 client preface ("PRI * HTTP/2.0...").
    Always replies with a minimal valid HTTP/1.1 200 so the gateway's own
    relay logic doesn't error out regardless of which protocol it used."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.first_bytes: "list[bytes]" = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                data = conn.recv(4096)
                self.first_bytes.append(data)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def protocol_sniffer():
    sniffer = _RawSniffer()
    yield sniffer
    sniffer.close()


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


@pytest.fixture
def http20_disabled_all_mode_gateway(tmp_path_factory, protocol_sniffer):
    """HTTP20_ENABLED off, HTTP20_PROXY_MODE=all, upstream is a raw sniffer
    (not a real HTTP server) — regression test fixture for a bug where "all"
    ignored HTTP20_ENABLED and relayed to APP_UPSTREAM over HTTP/2 even
    though SITE_PORT never accepts HTTP/2 from a client when it's off."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_sniffer.port}",
        "SKIP_AUTH_ROUTES": "/grpctest",
        "HTTP20_ENABLED": "false",
        "HTTP20_PROXY_MODE": "all",
    })
    yield port
    _stop(proc)


@pytest.fixture(scope="module")
def appservice_port_gateway(tmp_path_factory, protocol_app):
    """APPSERVICE_HTTP20_ONLY_PORT set, but HTTP20_ENABLED/HTTP20_PROXY_MODE
    both off (and auth bypassed) — the dedicated port should still listen as
    HTTP/2 and relay to APP_UPSTREAM as HTTP/2 regardless, mirroring how
    Azure App Service's separate gRPC port is independent of the main site's
    HTTP/2 settings."""
    proc, port, dedicated_port = _start_gateway_with_appservice_port(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['grpc_port']}",
        "SKIP_AUTH_ROUTES": "/echo\\.Echo/",
        "HTTP20_ENABLED": "false",
        "HTTP20_PROXY_MODE": "disabled",
    })
    yield port, dedicated_port
    _stop(proc)


@pytest.fixture(scope="module")
def appservice_port_protected_gateway(tmp_path_factory, protocol_app):
    """Same shape as appservice_port_gateway but without SKIP_AUTH_ROUTES, so
    requests go through the normal Easy Auth check — and fail it, since no
    real IdP session is ever presented in these tests."""
    proc, port, dedicated_port = _start_gateway_with_appservice_port(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['grpc_port']}",
        "HTTP20_ENABLED": "false",
        "HTTP20_PROXY_MODE": "disabled",
    })
    yield port, dedicated_port
    _stop(proc)


@pytest.fixture(scope="module")
def appservice_grpc_only_gateway(tmp_path_factory, protocol_app):
    """APPSERVICE_HTTP20_ONLY_PORT set together with HTTP20_PROXY_MODE=grpc-only
    — SITE_PORT should stop Content-Type-detecting gRPC (it's expected to
    arrive via the dedicated port instead) and downgrade everything to
    HTTP/1.1, which breaks a call against this HTTP/2-only upstream."""
    proc, port, dedicated_port = _start_gateway_with_appservice_port(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['grpc_port']}",
        "SKIP_AUTH_ROUTES": "/echo\\.Echo/",
        "HTTP20_ENABLED": "true",
        "HTTP20_PROXY_MODE": "grpc-only",
    })
    yield port, dedicated_port
    _stop(proc)


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


def test_sse_streamed_incrementally_through_gateway(gateway_port):
    """The verification app sends one SSE event per second
    (SSE_EVENT_COUNT=10 by default) with no Content-Length, relying on
    connection-close to mark the end. A real incremental relay delivers each
    one promptly; a buffered relay would hang until all 10 arrive (~10s)
    before sending anything at all."""
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

        # A second read should also arrive promptly — proof of genuine
        # incremental delivery, not just a first chunk that happened to
        # include the response headers plus a coincidentally-fast reply.
        try:
            second_bytes = sock.recv(4096)
        except socket.timeout:
            pytest.fail("no further data arrived within 3s of the first chunk — looks buffered, not streamed")
        assert second_bytes, "connection closed after the first chunk with no further data"


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


def test_http20_disabled_forces_http1_upstream_even_when_proxy_mode_is_all(
    http20_disabled_all_mode_gateway, protocol_sniffer,
):
    """Regression test: HTTP20_PROXY_MODE=all used to relay to APP_UPSTREAM
    over HTTP/2 unconditionally, even with HTTP20_ENABLED off — upgrading a
    request that was never HTTP/2 to begin with. It must now stay HTTP/1.1."""
    port = http20_disabled_all_mode_gateway
    request = (
        "POST /grpctest HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/grpc\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode())
        sock.settimeout(5)
        sock.recv(4096)  # drain the response; the sniffer's capture is what matters
    assert protocol_sniffer.first_bytes, "the gateway never reached APP_UPSTREAM"
    first_line = protocol_sniffer.first_bytes[0].split(b"\r\n", 1)[0]
    assert first_line.startswith(b"POST /grpctest HTTP/1.1"), (
        f"expected an HTTP/1.1 relay (HTTP20_ENABLED is off) despite "
        f"HTTP20_PROXY_MODE=all, got: {first_line!r}"
    )


def test_appservice_dedicated_port_listens_http2_even_when_http20_disabled(appservice_port_gateway):
    """Listen side: APPSERVICE_HTTP20_ONLY_PORT must speak HTTP/2 regardless
    of HTTP20_ENABLED (mirrors Azure App Service's HTTP20_ONLY_PORT being a
    separate, always-HTTP/2 port, independent of the main site setting)."""
    _, dedicated_port = appservice_port_gateway
    status, _, body = send_http2("127.0.0.1", dedicated_port, "GET", "/healthz", [], None)
    assert status == "200"
    assert body == b"ok"


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_appservice_dedicated_port_relays_grpc_regardless_of_proxy_mode(appservice_port_gateway):
    """Relay side: requests via the dedicated port always reach APP_UPSTREAM
    over HTTP/2, regardless of HTTP20_PROXY_MODE (here "disabled", which
    would downgrade everything on SITE_PORT)."""
    _, dedicated_port = appservice_port_gateway
    channel = grpc.insecure_channel(f"127.0.0.1:{dedicated_port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        response = stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
        assert "world" in response.message
    finally:
        channel.close()


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_unauthenticated_grpc_via_dedicated_port_fails_fast(appservice_port_protected_gateway):
    """Regression test: an unauthenticated gRPC call used to hang until its
    own deadline, because the gateway replied with a browser-style redirect
    to /.auth/login that a gRPC client can't follow. It must now fail fast."""
    _, dedicated_port = appservice_port_protected_gateway
    channel = grpc.insecure_channel(f"127.0.0.1:{dedicated_port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        start = time.monotonic()
        with pytest.raises(grpc.RpcError):
            stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
        elapsed = time.monotonic() - start
        # The old bug hung all the way to GRPC_TIMEOUT (5s). Fixed, this
        # consistently takes ~2s (grpc-python's own client-side retry/backoff
        # overhead on top of the gateway's immediate 401) — well short of the
        # deadline, which is the distinction this test cares about.
        assert elapsed < 4.0, f"took {elapsed:.1f}s — looks like it hung instead of failing fast"
    finally:
        channel.close()


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_site_port_stops_detecting_grpc_when_dedicated_port_is_set(appservice_grpc_only_gateway):
    """Once APPSERVICE_HTTP20_ONLY_PORT is set, SITE_PORT stops preserving
    HTTP/2 for gRPC-shaped requests even under HTTP20_PROXY_MODE=grpc-only —
    gRPC is expected to arrive via the dedicated port instead, so this call
    gets downgraded to HTTP/1.1 and fails against the HTTP/2-only upstream."""
    site_port, _ = appservice_grpc_only_gateway
    channel = grpc.insecure_channel(f"127.0.0.1:{site_port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        with pytest.raises(grpc.RpcError):
            stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
    finally:
        channel.close()


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_dedicated_port_still_works_when_site_port_downgrades(appservice_grpc_only_gateway):
    """The other half of the same scenario: the dedicated port itself keeps
    working normally even while SITE_PORT downgrades gRPC to HTTP/1.1."""
    _, dedicated_port = appservice_grpc_only_gateway
    channel = grpc.insecure_channel(f"127.0.0.1:{dedicated_port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        response = stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
        assert "world" in response.message
    finally:
        channel.close()


def test_appservice_port_same_as_site_port_fails_fast(tmp_path_factory):
    """Startup validation: APPSERVICE_HTTP20_ONLY_PORT must differ from
    SITE_PORT — the gateway should exit immediately rather than trying (and
    failing) to bind the same port twice."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "src/app.py", "--config", str(_empty_config(tmp_path_factory))],
        cwd=REPO_ROOT,
        env={**os.environ, "SITE_PORT": str(port), "APPSERVICE_HTTP20_ONLY_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        returncode = proc.wait(timeout=5)
        assert returncode != 0
    finally:
        _stop(proc)

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

APPSERVICE_HTTP20_ONLY_PORT (Azure App Service's dedicated gRPC port app
setting) is confirmed against a real App Service instance to be purely an
upstream-side routing detail — clients always dial the ordinary endpoint,
and the platform internally forwards HTTP/2-relayed traffic to that port on
APP_UPSTREAM's own host instead of its regular port (see ToDo.md). Covered by
their own fixtures below:
- test_http20_disabled_forces_http1_upstream_even_when_proxy_mode_is_all:
  HTTP20_PROXY_MODE=all used to force an HTTP/2 upstream relay even when
  HTTP20_ENABLED was off (SITE_PORT never accepts HTTP/2 from a client in
  that case, so there was nothing to "preserve").
- test_unauthenticated_grpc_with_http20_only_port_fails_fast: an
  unauthenticated gRPC call used to hang until its own deadline, because the
  gateway replied with a browser-style redirect to /.auth/login that a gRPC
  client can't follow.
- test_appservice_http20_only_port_relays_grpc_to_separate_upstream_port /
  test_grpc_content_routes_to_http20_only_port_not_app_upstream /
  test_ordinary_request_still_routes_to_app_upstream_with_http20_only_port_set /
  test_all_mode_routes_ordinary_request_to_http20_only_port_too: the port
  redirection itself, and which content it does (and doesn't) apply to.

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

import h2.config
import h2.connection
import h2.events
import h2.settings

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
    port (WebSocket/SSE/chunked-body test endpoints, plus the same
    principal/storage pages as src/sample_app.py) and its own gRPC port,
    independent of any gateway instance. --config points at an empty file —
    without it, the shared config-loading module (src/_sample_app_shared.py)
    falls back to reading ./config.toml (a real, secret-bearing dev config)
    when run with cwd=REPO_ROOT, same rationale as _empty_config's other use
    below for the gateway itself."""
    http_port = _free_port()
    grpc_port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.protocol.app", "--config", str(_empty_config(tmp_path_factory))],
        cwd=REPO_ROOT,
        env={**os.environ, "SAMPLE_APP_PORT": str(http_port), "PROTOCOL_APP_GRPC_PORT": str(grpc_port)},
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
def http2_client_gateway_port(tmp_path_factory, protocol_app):
    """HTTP20_ENABLED=true (client-facing HTTP/2), upstream is the
    verification app's plain HTTP/1.1 port. HTTP20_PROXY_MODE is deliberately
    left at its default (disabled): confirmed against a real Azure App
    Service instance (tests/protocol/azure-websocket-poc) that an RFC 8441
    WebSocket bootstrap always relays to APP_UPSTREAM as a classic HTTP/1.1
    Upgrade regardless of that setting."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['http_port']}",
        "SKIP_AUTH_ROUTES": "/ws/",
        "HTTP20_ENABLED": "true",
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
def websockets_disabled_gateway(tmp_path_factory, protocol_sniffer):
    """WEB_SOCKETS_ENABLED=false (mirrors Azure App Service's own
    webSocketsEnabled=false — confirmed on real Linux App Service to have no
    effect there, but this setting exists in the emulator for Windows App
    Service fidelity), HTTP20_ENABLED=true so both the HTTP/1.1 and RFC 8441
    bootstrap paths are reachable, upstream is a raw sniffer so a request
    that does get relayed is answered immediately rather than hanging."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_sniffer.port}",
        "SKIP_AUTH_ROUTES": "/ws/",
        "HTTP20_ENABLED": "true",
        "WEB_SOCKETS_ENABLED": "false",
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
def appservice_grpc_dual_port_gateway(tmp_path_factory, protocol_app):
    """Mirrors real Azure App Service's HTTP20_ONLY_PORT: a single
    client-facing SITE_PORT (confirmed against a real App Service instance
    — see ToDo.md — that clients always dial the ordinary endpoint; there is
    no separate client-facing port). APP_UPSTREAM points at the verification
    app's regular HTTP/1.1 port; APPSERVICE_HTTP20_ONLY_PORT points at its
    separate real gRPC port on the same host — the app needs two listeners,
    same as a real App Service gRPC app does."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['http_port']}",
        "APPSERVICE_HTTP20_ONLY_PORT": str(protocol_app["grpc_port"]),
        "SKIP_AUTH_ROUTES": "/echo\\.Echo/",
        "HTTP20_ENABLED": "true",
        "HTTP20_PROXY_MODE": "grpc-only",
    })
    yield port
    _stop(proc)


@pytest.fixture(scope="module")
def appservice_grpc_dual_port_protected_gateway(tmp_path_factory, protocol_app):
    """Same shape as appservice_grpc_dual_port_gateway but without
    SKIP_AUTH_ROUTES, so requests go through the normal Easy Auth check —
    and fail it, since no real IdP session is ever presented in these
    tests."""
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{protocol_app['http_port']}",
        "APPSERVICE_HTTP20_ONLY_PORT": str(protocol_app["grpc_port"]),
        "HTTP20_ENABLED": "true",
        "HTTP20_PROXY_MODE": "grpc-only",
    })
    yield port
    _stop(proc)


@pytest.fixture
def dual_sniffer_gateway(tmp_path_factory):
    """Two raw sniffers standing in for APP_UPSTREAM's own port and
    APPSERVICE_HTTP20_ONLY_PORT respectively — lets a test observe directly
    which port a relay attempt actually reaches, independent of whether
    either end actually speaks real HTTP/2 or gRPC."""
    regular = _RawSniffer()
    dedicated = _RawSniffer()
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{regular.port}",
        "APPSERVICE_HTTP20_ONLY_PORT": str(dedicated.port),
        "SKIP_AUTH_ROUTES": "/echo\\.Echo/,/ordinary",
        "HTTP20_ENABLED": "true",
        "HTTP20_PROXY_MODE": "grpc-only",
    })
    yield port, regular, dedicated
    _stop(proc)
    regular.close()
    dedicated.close()


@pytest.fixture
def dual_sniffer_all_mode_gateway(tmp_path_factory):
    """Same as dual_sniffer_gateway but HTTP20_PROXY_MODE=all: every request
    is relayed as HTTP/2 regardless of Content-Type, so with
    APPSERVICE_HTTP20_ONLY_PORT set, everything should route there instead
    of APP_UPSTREAM's own port — not just gRPC-shaped requests."""
    regular = _RawSniffer()
    dedicated = _RawSniffer()
    proc, port = _start_gateway(tmp_path_factory, {
        "APP_UPSTREAM": f"http://127.0.0.1:{regular.port}",
        "APPSERVICE_HTTP20_ONLY_PORT": str(dedicated.port),
        "SKIP_AUTH_ROUTES": "/ordinary",
        "HTTP20_ENABLED": "true",
        "HTTP20_PROXY_MODE": "all",
    })
    yield port, regular, dedicated
    _stop(proc)
    regular.close()
    dedicated.close()


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


def _websocket_over_http2_rfc8441(port: int, extra_headers: "list[tuple[str, str]]") -> "tuple[bytes, bytes]":
    """Bootstrap a WebSocket over HTTP/2 via an RFC 8441 extended CONNECT
    (:method CONNECT, :protocol websocket), send one text frame, and return
    (status, echoed_bytes)."""
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(conn.data_to_send())

        # A client must see the server advertise ENABLE_CONNECT_PROTOCOL
        # before attempting an extended CONNECT (RFC 8441 section 3) — h2
        # itself enforces this locally, so wait for that SETTINGS frame.
        while not conn.remote_settings.get(h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL, 0):
            data = sock.recv(65536)
            assert data, "connection closed before advertising ENABLE_CONNECT_PROTOCOL"
            conn.receive_data(data)
            out = conn.data_to_send()
            if out:
                sock.sendall(out)

        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(stream_id, [
            (":method", "CONNECT"),
            (":protocol", "websocket"),
            (":scheme", "http"),
            (":authority", f"127.0.0.1:{port}"),
            (":path", "/ws/echo"),
            ("sec-websocket-version", "13"),
        ] + extra_headers)
        sock.sendall(conn.data_to_send())

        payload = b"hello"
        mask = b"\x01\x02\x03\x04"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        ws_frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked

        status = None
        sent_frame = False
        body = b""
        for _ in range(20):
            data = sock.recv(65536)
            if not data:
                break
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    status = dict(event.headers).get(b":status")
                elif isinstance(event, h2.events.DataReceived):
                    body += event.data
                    conn.acknowledge_received_data(len(event.data), event.stream_id)
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
            if status == b"200" and not sent_frame:
                sent_frame = True
                conn.send_data(stream_id, ws_frame)
                sock.sendall(conn.data_to_send())
            if len(body) >= 2:
                break
        return status, body


def test_websocket_upgrade_over_http2_rfc8441_through_gateway(http2_client_gateway_port):
    """RFC 8441: bootstrap WebSocket over HTTP/2 via an extended CONNECT
    (:method CONNECT, :protocol websocket) instead of the classic HTTP/1.1
    Upgrade. Confirmed against a real Azure App Service instance
    (tests/protocol/azure-websocket-poc) that it relays this to the backend
    as a classic HTTP/1.1 Upgrade regardless of HTTP20_PROXY_MODE, so the
    gateway's _Http2StreamHandler does the same and this test expects the
    same echo behavior as the HTTP/1.1 test above, just bootstrapped
    differently."""
    port = http2_client_gateway_port
    key = base64.b64encode(b"0123456789012345").decode()
    payload = b"hello"
    status, body = _websocket_over_http2_rfc8441(port, [("sec-websocket-key", key)])
    assert status == b"200", f"expected :status 200 for the extended CONNECT, got {status!r}"
    assert body == bytes([0x81, len(payload)]) + payload, f"unexpected echo: {body!r}"


def test_websocket_over_http2_rfc8441_without_sec_websocket_key(http2_client_gateway_port):
    """A real browser's extended CONNECT omits Sec-WebSocket-Key entirely
    (confirmed via DevTools against Edge/Chrome) — RFC 8441 has no need for
    RFC 6455's classic handshake nonce, since HTTP/2 doesn't have the
    cross-protocol confusion risk that nonce defends against. The upstream
    verification app still expects one (like any ordinary RFC 6455 server
    would), so the gateway must synthesize it when relaying to APP_UPSTREAM's
    classic HTTP/1.1 Upgrade — without this, the upstream rejects the
    synthesized Upgrade request with 400, which is exactly what happened the
    first time this was tried against a real browser instead of a raw h2
    client that (unlike a browser) happened to always send the header."""
    port = http2_client_gateway_port
    payload = b"hello"
    status, body = _websocket_over_http2_rfc8441(port, [])
    assert status == b"200", f"expected :status 200 for the extended CONNECT, got {status!r}"
    assert body == bytes([0x81, len(payload)]) + payload, f"unexpected echo: {body!r}"


def test_websocket_disabled_falls_back_to_ordinary_proxy_over_http1(websockets_disabled_gateway, protocol_sniffer):
    """WEB_SOCKETS_ENABLED=false: an HTTP/1.1 Upgrade request must not be
    treated specially — it falls through to the ordinary _proxy_to path,
    which strips Connection/Upgrade as hop-by-hop, same as any other
    request. The sniffer's fixed 200 OK (rather than a 101, or the relay
    hanging waiting for bytes that never come) is proof of that."""
    port = websockets_disabled_gateway
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
        sock.settimeout(5)
        response = sock.recv(4096)
    assert response.startswith(b"HTTP/1.1 200"), (
        f"expected an ordinary 200 (WEB_SOCKETS_ENABLED=false), got: {response[:200]!r}"
    )
    assert protocol_sniffer.first_bytes, "the gateway never reached APP_UPSTREAM"
    upstream_request = protocol_sniffer.first_bytes[0]
    assert b"Upgrade:" not in upstream_request, (
        f"Connection/Upgrade should be stripped as hop-by-hop like any other request, "
        f"got: {upstream_request[:300]!r}"
    )


def test_websocket_disabled_does_not_advertise_connect_protocol_over_http2(websockets_disabled_gateway):
    """WEB_SOCKETS_ENABLED=false: the gateway's HTTP/2 server must not
    advertise SETTINGS_ENABLE_CONNECT_PROTOCOL, so a compliant client never
    even attempts the RFC 8441 extended CONNECT bootstrap in the first
    place (same reasoning as h2's own un-configured default, just now an
    explicit choice instead of an accident)."""
    port = websockets_disabled_gateway
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(conn.data_to_send())
        data = sock.recv(65536)
        assert data, "connection closed before sending SETTINGS"
        conn.receive_data(data)
    assert conn.remote_settings.get(h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL, 0) == 0


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


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_appservice_http20_only_port_relays_grpc_to_separate_upstream_port(appservice_grpc_dual_port_gateway):
    """The corrected behavior: a real gRPC call through the single
    client-facing SITE_PORT gets relayed to APPSERVICE_HTTP20_ONLY_PORT on
    APP_UPSTREAM's host (the verification app's real gRPC server) — not to
    APP_UPSTREAM's own port (its plain HTTP/1.1 port, which doesn't speak
    gRPC at all). Confirmed against a real Azure App Service instance that
    this is exactly the two-listener shape a real gRPC app needs there too
    (see ToDo.md)."""
    port = appservice_grpc_dual_port_gateway
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        stub = echo_pb2_grpc.EchoStub(channel)
        response = stub.SayHello(echo_pb2.HelloRequest(name="world"), timeout=GRPC_TIMEOUT)
        assert "world" in response.message
    finally:
        channel.close()


@pytest.mark.skipif(not HAS_GRPC, reason="grpcio not installed — see requirements-test.txt")
def test_unauthenticated_grpc_with_http20_only_port_fails_fast(appservice_grpc_dual_port_protected_gateway):
    """Regression coverage carried over from the old (incorrect) dedicated
    client-facing-port design: an unauthenticated gRPC call must fail fast
    (401), not hang until its own deadline waiting on a browser-style
    redirect it can't follow."""
    port = appservice_grpc_dual_port_protected_gateway
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
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


def test_grpc_content_routes_to_http20_only_port_not_app_upstream(dual_sniffer_gateway):
    """Content detected as gRPC (Content-Type: application/grpc*) reaches
    APPSERVICE_HTTP20_ONLY_PORT on APP_UPSTREAM's host, not APP_UPSTREAM's
    own port."""
    port, regular, dedicated = dual_sniffer_gateway
    request = (
        "POST /echo.Echo/SayHello HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/grpc\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode())
        sock.settimeout(5)
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
    assert dedicated.first_bytes, "expected the gRPC-shaped request to reach APPSERVICE_HTTP20_ONLY_PORT"
    assert not regular.first_bytes, "APP_UPSTREAM's own port should not have been contacted for gRPC content"


def test_ordinary_request_still_routes_to_app_upstream_with_http20_only_port_set(dual_sniffer_gateway):
    """Non-gRPC content still reaches APP_UPSTREAM's own port even with
    APPSERVICE_HTTP20_ONLY_PORT configured — only content detected as gRPC
    gets redirected to the dedicated port (HTTP20_PROXY_MODE=grpc-only)."""
    port, regular, dedicated = dual_sniffer_gateway
    request = (
        "GET /ordinary HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode())
        sock.settimeout(5)
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
    assert regular.first_bytes, "expected the ordinary request to reach APP_UPSTREAM's own port"
    assert not dedicated.first_bytes, "APPSERVICE_HTTP20_ONLY_PORT should not have been contacted for ordinary content"


def test_all_mode_routes_ordinary_request_to_http20_only_port_too(dual_sniffer_all_mode_gateway):
    """HTTP20_PROXY_MODE=all relays everything as HTTP/2 regardless of
    Content-Type, so with APPSERVICE_HTTP20_ONLY_PORT set, even an ordinary
    (non-gRPC) request routes to the dedicated port, not APP_UPSTREAM's own
    port."""
    port, regular, dedicated = dual_sniffer_all_mode_gateway
    request = (
        "GET /ordinary HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode())
        sock.settimeout(5)
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
    assert dedicated.first_bytes, "expected HTTP20_PROXY_MODE=all to route even ordinary content to APPSERVICE_HTTP20_ONLY_PORT"
    assert not regular.first_bytes

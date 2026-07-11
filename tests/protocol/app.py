"""
Manual verification app for protocol gaps tracked in ToDo.md:
WebSocket, gRPC, SSE/streaming, and chunked request bodies. Also includes
the same principal/claims/storage verification pages as src/sample_app.py
(the "lite", distributed edition of this same demo-APP_UPSTREAM role) - this
is the "full" dev-only edition, with gRPC added on top.

Run standalone (not wired into start.py):
    python -m tests.protocol.app

HTTP port defaults to 8082 (like src/sample_app.py, but set to a different
default so both can run side by side without colliding); override with
SAMPLE_APP_PORT in config.toml or as an env var - the same setting
src/sample_app.py uses, since both are the same demo-APP_UPSTREAM role.

Then either:
- Open http://localhost:8082/ directly to see each feature work.
- Set APP_UPSTREAM=http://localhost:8082 in the emulator's config.toml,
  start the emulator, and open the same page through the gateway
  (SITE_PORT) to see which features break when proxied.

See README.md in this directory for gRPC verification (grpcurl) instructions.
"""

import json
import os
import sys
from concurrent import futures
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grpc  # noqa: E402
from grpc_reflection.v1alpha import reflection  # noqa: E402

import echo_pb2  # noqa: E402
import echo_pb2_grpc  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
import _sample_app_shared as shared  # noqa: E402

HTTP_PORT = int(shared._cfg("SAMPLE_APP_PORT", "8082"))
GRPC_PORT = int(os.environ.get("PROTOCOL_APP_GRPC_PORT", "8083"))


# ---------------------------------------------------------------------------
# gRPC service
# ---------------------------------------------------------------------------

class _EchoServicer(echo_pb2_grpc.EchoServicer):
    def SayHello(self, request, context):
        return echo_pb2.HelloReply(
            message=f"Hello, {request.name}! (answered directly by the protocol "
                    f"verification app's gRPC service on port {GRPC_PORT})"
        )


def _start_grpc_server() -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    echo_pb2_grpc.add_EchoServicer_to_server(_EchoServicer(), server)
    service_names = (
        echo_pb2.DESCRIPTOR.services_by_name["Echo"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)
    server.add_insecure_port(f"0.0.0.0:{GRPC_PORT}")
    server.start()
    return server


def _grpc_section_html() -> str:
    grpc_target_direct = f"localhost:{GRPC_PORT}"
    return f"""<section>
  <h2>gRPC</h2>
  <p class="muted">Browsers cannot call raw gRPC directly. Run these with
  <a href="https://github.com/fullstorydev/grpcurl" target="_blank" rel="noopener">grpcurl</a> from a terminal.</p>
  <p><strong>Direct (should succeed):</strong></p>
  <pre class="cmd">grpcurl -plaintext -d '{{"name":"world"}}' {grpc_target_direct} echo.Echo/SayHello</pre>
  <p><strong>Through the gateway (replace &lt;gateway-port&gt; with SITE_PORT — expected to fail):</strong></p>
  <pre class="cmd">grpcurl -plaintext -d '{{"name":"world"}}' localhost:&lt;gateway-port&gt; echo.Echo/SayHello</pre>
</section>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler, shared.ProtocolDemoMixin):
    server_version = "ProtocolGapVerificationApp/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self) -> dict[str, str]:
        return {key: value for key, value in self.headers.items()}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=False)
        storage_flow = qs.get("storage_flow", ["direct"])[0].lower()
        if storage_flow not in {"obo", "direct"}:
            storage_flow = "obo"

        if path == "/":
            self._send_html(shared.render_protocol_demo_page(HTTP_PORT, extra_sections_html=_grpc_section_html()))
            return

        if path == "/session":
            auth = shared.principal_summary(self._headers())
            storage = shared.storage_preview(self._headers(), storage_flow)
            self._send_html(shared.render_html(
                auth, storage,
                extra_quick_action='<a class="button ghost" href="/">Protocol demo</a>',
            ))
            return

        if path == "/healthz":
            self._send_text("ok")
            return

        if path == "/ws/echo":
            self._handle_ws_echo()
            return

        if path == "/sse/stream":
            self._handle_sse()
            return

        if path == "/api/session":
            self._send_json(shared.principal_summary(self._headers()))
            return

        if path == "/api/storage":
            self._send_json(shared.storage_preview(self._headers(), storage_flow))
            return

        if path == "/api/report":
            auth = shared.principal_summary(self._headers())
            storage = shared.storage_preview(self._headers(), storage_flow)
            self._send_json(shared.build_report(auth, storage))
            return

        if path == "/api/recommendations":
            self._send_json(shared.RECOMMENDATIONS)
            return

        self._send_text("not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/chunked/echo":
            self._handle_chunked_echo()
        else:
            self._send_text("not found", status=404)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, markup: str, status: int = 200) -> None:
        body = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    grpc_server = _start_grpc_server()
    print(f"gRPC service   : localhost:{GRPC_PORT} (direct, reflection enabled)")
    print(f"HTTP verify app: http://localhost:{HTTP_PORT}/")
    print("Press Ctrl+C to stop.")
    http_server = shared.QuietErrorThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        http_server.server_close()
        grpc_server.stop(grace=0)


if __name__ == "__main__":
    main()

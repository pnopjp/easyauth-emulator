"""
Manual verification app for protocol gaps tracked in ToDo.md:
WebSocket, gRPC, SSE/streaming, and chunked request bodies.

Run standalone (not wired into start.py):
    python -m tests.protocol.app

Then either:
- Open http://localhost:<PROTOCOL_APP_PORT>/ directly to see each feature work.
- Set APP_UPSTREAM=http://localhost:<PROTOCOL_APP_PORT> in the emulator's
  config.toml, start the emulator, and open the same page through the
  gateway (SITE_PORT) to see which features break when proxied.

See README.md in this directory for gRPC verification (grpcurl) instructions.
"""

import base64
import hashlib
import html
import json
import os
import sys
import time
from concurrent import futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grpc  # noqa: E402
from grpc_reflection.v1alpha import reflection  # noqa: E402

import echo_pb2  # noqa: E402
import echo_pb2_grpc  # noqa: E402

HTTP_PORT = int(os.environ.get("PROTOCOL_APP_PORT", "8082"))
GRPC_PORT = int(os.environ.get("PROTOCOL_APP_GRPC_PORT", "8083"))
SSE_INTERVAL_SECONDS = float(os.environ.get("PROTOCOL_APP_SSE_INTERVAL_SECONDS", "1"))
SSE_EVENT_COUNT = int(os.environ.get("PROTOCOL_APP_SSE_COUNT", "10"))

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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


# ---------------------------------------------------------------------------
# WebSocket handshake and framing (RFC 6455) — no fragmentation/extension support,
# enough for a single-frame echo demo.
# ---------------------------------------------------------------------------

def _ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

def _render_page() -> str:
    grpc_target_direct = f"localhost:{GRPC_PORT}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Protocol gap verification app</title>
<style>
  body {{ font-family: "Segoe UI", sans-serif; max-width: 900px; margin: 32px auto; padding: 0 16px; color: #10213a; }}
  h1 {{ margin-bottom: 4px; }}
  .muted {{ color: #5a6b85; }}
  section {{ border: 1px solid rgba(16,33,58,0.14); border-radius: 12px; padding: 16px 20px; margin: 20px 0; }}
  h2 {{ margin-top: 0; }}
  button {{ padding: 8px 14px; border-radius: 8px; border: 1px solid #1847d6; background: #1847d6; color: white; cursor: pointer; }}
  input[type=text] {{ padding: 6px 8px; border-radius: 6px; border: 1px solid rgba(16,33,58,0.24); width: 260px; }}
  pre.log {{ background: #10213a; color: #d9e4ff; padding: 12px; border-radius: 8px; height: 160px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}
  code {{ background: rgba(16,33,58,0.06); padding: 2px 6px; border-radius: 4px; }}
  pre.cmd {{ background: rgba(16,33,58,0.06); padding: 10px 12px; border-radius: 8px; overflow-x: auto; }}
</style>
</head>
<body>
<h1>Protocol gap verification app</h1>
<p class="muted">Load this page directly (this app's own port) to see each feature work, then load it again through the
EasyAuth Emulator gateway (after setting <code>APP_UPSTREAM</code> to this app's HTTP port) to see which features
break when proxied. See <code>tests/protocol/README.md</code> for setup and gRPC instructions.</p>

<section>
  <h2>WebSocket</h2>
  <button onclick="wsConnect()">Connect</button>
  <button onclick="wsDisconnect()">Disconnect</button>
  <input type="text" id="wsInput" value="hello" />
  <button onclick="wsSend()">Send</button>
  <pre class="log" id="wsLog"></pre>
</section>

<section>
  <h2>SSE / streaming</h2>
  <p class="muted">Expects one event roughly every {SSE_INTERVAL_SECONDS}s. If they all arrive at once at the end,
  the response is being buffered instead of streamed.</p>
  <button onclick="sseStart()">Start stream</button>
  <pre class="log" id="sseLog"></pre>
</section>

<section>
  <h2>Chunked request body</h2>
  <p class="muted">Browsers cannot send a <code>Transfer-Encoding: chunked</code> request body to an HTTP/1.1
  server: Chrome/Edge require HTTP/2 or HTTP/3 for streaming <code>fetch</code> request bodies and throw
  <code>net::ERR_H2_OR_QUIC_REQUIRED</code> otherwise, and this app (like the gateway) only speaks HTTP/1.1. Use
  the curl command from a terminal instead.</p>
  <pre class="cmd">curl -X POST --no-buffer -H "Transfer-Encoding: chunked" --data-binary "chunked request body test" &lt;base-url&gt;/chunked/echo</pre>
  <p class="muted">Replace <code>&lt;base-url&gt;</code> with <code>{f"http://localhost:{HTTP_PORT}"}</code> (direct) or the gateway's <code>SITE_URL:SITE_PORT</code> (via proxy).</p>
</section>

<section>
  <h2>gRPC</h2>
  <p class="muted">Browsers cannot call raw gRPC directly. Run these with
  <a href="https://github.com/fullstorydev/grpcurl" target="_blank" rel="noopener">grpcurl</a> from a terminal.</p>
  <p><strong>Direct (should succeed):</strong></p>
  <pre class="cmd">grpcurl -plaintext -d '{{"name":"world"}}' {grpc_target_direct} echo.Echo/SayHello</pre>
  <p><strong>Through the gateway (replace &lt;gateway-port&gt; with SITE_PORT — expected to fail):</strong></p>
  <pre class="cmd">grpcurl -plaintext -d '{{"name":"world"}}' localhost:&lt;gateway-port&gt; echo.Echo/SayHello</pre>
</section>

<script>
function log(id, text) {{
  const el = document.getElementById(id);
  el.textContent += text + "\\n";
  el.scrollTop = el.scrollHeight;
}}

let ws = null;
function wsConnect() {{
  const url = location.origin.replace(/^http/, "ws") + "/ws/echo";
  log("wsLog", "connecting to " + url);
  ws = new WebSocket(url);
  ws.onopen = () => log("wsLog", "[open]");
  ws.onmessage = (e) => log("wsLog", "recv: " + e.data);
  ws.onerror = () => log("wsLog", "[error]");
  ws.onclose = () => log("wsLog", "[closed]");
}}
function wsSend() {{
  if (!ws || ws.readyState !== 1) {{ log("wsLog", "[not connected]"); return; }}
  const value = document.getElementById("wsInput").value;
  ws.send(value);
  log("wsLog", "sent: " + value);
}}
function wsDisconnect() {{
  if (!ws) {{ log("wsLog", "[not connected]"); return; }}
  ws.close(1000, "manual disconnect");
}}

function sseStart() {{
  document.getElementById("sseLog").textContent = "";
  const es = new EventSource("/sse/stream");
  const startedAt = performance.now();
  es.onmessage = (e) => log("sseLog", `+${{Math.round(performance.now() - startedAt)}}ms: ${{e.data}}`);
  es.onerror = () => {{ log("sseLog", "[closed/error]"); es.close(); }};
}}

</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ProtocolGapVerificationApp/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send_html(_render_page())
        elif self.path == "/healthz":
            self._send_text("ok")
        elif self.path == "/ws/echo":
            self._handle_ws_echo()
        elif self.path == "/sse/stream":
            self._handle_sse()
        else:
            self._send_text("not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/chunked/echo":
            self._handle_chunked_echo()
        else:
            self._send_text("not found", status=404)

    # -- WebSocket -----------------------------------------------------

    def _handle_ws_echo(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self._send_text("Expected a WebSocket Upgrade request.", status=400)
            return

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept_key(key))
        self.end_headers()

        try:
            while True:
                frame = self._ws_read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    self._ws_write_frame(0x8, payload)
                    break
                if opcode == 0x9:  # ping -> pong
                    self._ws_write_frame(0xA, payload)
                    continue
                if opcode in (0x1, 0x2):  # text/binary -> echo
                    self._ws_write_frame(opcode, payload)
        except (ConnectionError, OSError):
            pass

    def _ws_read_frame(self) -> "tuple[int, bytes] | None":
        header = self.rfile.read(2)
        if len(header) < 2:
            return None
        b1, b2 = header
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(self.rfile.read(2), "big")
        elif length == 127:
            length = int.from_bytes(self.rfile.read(8), "big")
        mask_key = self.rfile.read(4) if masked else b""
        payload = self.rfile.read(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _ws_write_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + length.to_bytes(2, "big")
        else:
            header += bytes([127]) + length.to_bytes(8, "big")
        self.wfile.write(header + payload)
        self.wfile.flush()

    # -- SSE -------------------------------------------------------------

    def _handle_sse(self) -> None:
        # Closing the connection when the stream ends (rather than keeping it
        # alive for a next request) lets a client with no Content-Length know
        # where the response ends without waiting on its own read timeout.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for i in range(SSE_EVENT_COUNT):
                chunk = f"data: tick {i} at {time.time():.3f}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
        except (ConnectionError, OSError):
            pass

    # -- chunked request body --------------------------------------------

    def _read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._read_chunked_body()
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _read_chunked_body(self) -> bytes:
        chunks = []
        while True:
            size_line = self.rfile.readline().strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    def _handle_chunked_echo(self) -> None:
        body = self._read_body()
        try:
            preview = body.decode("utf-8")
        except UnicodeDecodeError:
            preview = base64.b64encode(body).decode("ascii")
        self._send_json({
            "received_bytes": len(body),
            "transfer_encoding": self.headers.get("Transfer-Encoding", "(none)"),
            "content_length_header": self.headers.get("Content-Length", "(none)"),
            "preview": preview,
        })

    # -- response helpers --------------------------------------------------

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
    http_server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        http_server.server_close()
        grpc_server.stop(grace=0)


if __name__ == "__main__":
    main()

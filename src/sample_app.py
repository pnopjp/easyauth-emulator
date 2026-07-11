from __future__ import annotations

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _sample_app_shared as shared  # noqa: E402


def _cfg(key: str, default: str = "") -> str:
    return shared._cfg(key, default)


class Handler(BaseHTTPRequestHandler, shared.ProtocolDemoMixin):
    server_version = "EasyAuthVerificationApp/1.0"

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

        if path in {"/", "/index.html"}:
            auth = shared.principal_summary(self._headers())
            storage = shared.storage_preview(self._headers(), storage_flow)
            self._send_html(shared.render_html(
                auth, storage,
                extra_quick_action='<a class="button ghost" href="/protocol">Protocol demo</a>',
            ))
            return

        if path == "/protocol":
            self._send_html(shared.render_protocol_demo_page(_HTTP_PORT, back_url="/"))
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

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/chunked/echo":
            self._handle_chunked_echo()
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, markup: str, status: int = 200) -> None:
        body = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


_HTTP_PORT = int(_cfg("SAMPLE_APP_PORT", "8081") or "8081")


def main() -> None:
    server = shared.QuietErrorThreadingHTTPServer(("0.0.0.0", _HTTP_PORT), Handler)
    print(f"{shared.APP_TITLE} listening on http://0.0.0.0:{_HTTP_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()

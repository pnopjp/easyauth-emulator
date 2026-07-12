"""
Minimal gRPC app for deployment to Azure App Service (Linux, Python), used to
empirically verify whether Easy Auth protects the HTTP20_ONLY_PORT gRPC
listener — see ../README.md ("Azure gRPC + Easy Auth PoC") for the procedure
and ToDo.md for why this matters (deciding the gRPC support architecture).

SayHello echoes back every piece of gRPC metadata it received, so a real
Easy Auth-injected principal (if any) would show up in the response.

Startup command on App Service: python app.py
"""

import os
import threading
from concurrent import futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
from grpc_reflection.v1alpha import reflection

import echo_pb2
import echo_pb2_grpc


class EchoServicer(echo_pb2_grpc.EchoServicer):
    def SayHello(self, request, context):
        metadata_lines = "\n".join(f"{key}: {value}" for key, value in context.invocation_metadata())
        message = f"Hello, {request.name}!\n--- gRPC metadata received by the app ---\n{metadata_lines or '(none)'}"
        return echo_pb2.HelloReply(message=message)


class _WarmupProbeHandler(BaseHTTPRequestHandler):
    """Answers App Service's HTTP/1.1 warmup probe on the platform's default
    port — required even though this app is gRPC-only, or App Service considers
    the container unhealthy and restarts it in a loop (see ../README.md)."""

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve_warmup_probe() -> None:
    # App Service's default HTTP port for a Python image, per the platform log
    # ("App port: 8000, Port selected by: Default for this image").
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _WarmupProbeHandler)
    print(f"Warmup probe HTTP server listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


def serve() -> None:
    threading.Thread(target=_serve_warmup_probe, daemon=True).start()

    port = os.environ.get("HTTP20_ONLY_PORT", "8585")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    echo_pb2_grpc.add_EchoServicer_to_server(EchoServicer(), server)
    service_names = (
        echo_pb2.DESCRIPTOR.services_by_name["Echo"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    print(f"gRPC server listening on 0.0.0.0:{port}", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()

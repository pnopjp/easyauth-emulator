# Protocol gap verification app

Manual verification app for the protocol gaps tracked in `ToDo.md`: WebSocket, gRPC,
SSE/streaming, and chunked request bodies. Standalone — not wired into `start.py` or
`config.toml` (unlike `src/sample_app.py`).

## Setup

Requires `grpcio`/`grpcio-reflection` from `requirements-test.txt` — the system/global
Python interpreter will not have these. Use the project's `.venv` or install into
whatever interpreter you run this with:

```powershell
# using the project's .venv (Windows PowerShell)
.venv\Scripts\Activate.ps1
pip install -r requirements-test.txt
```

```bash
# or into your current interpreter directly
pip install -r requirements-test.txt
```

## Run

```bash
python -m tests.protocol.app
```

Starts two servers (Ctrl+C stops both):

- HTTP verification page: `http://localhost:8082/` (`PROTOCOL_APP_PORT`)
- gRPC service with reflection enabled: `localhost:8083` (`PROTOCOL_APP_GRPC_PORT`)

## Verify directly (expected to work)

Open `http://localhost:8082/` and try the WebSocket and SSE sections — both should work.

The chunked-body section has no browser button: Chrome/Edge require HTTP/2 or HTTP/3 for
streaming `fetch` request bodies and throw `net::ERR_H2_OR_QUIC_REQUIRED` against an
HTTP/1.1 server, and this app (like the gateway) only speaks HTTP/1.1. Use the curl
command shown on the page instead:

```bash
curl -X POST --no-buffer -H "Transfer-Encoding: chunked" --data-binary "chunked request body test" http://localhost:8082/chunked/echo
```

curl already knows the full body length up front, so it sends it as a single chunk — real
chunked framing, but not multi-chunk streaming. To send the body split across several
chunks with a delay between each (closer to a real streaming client), use
`send_chunked.py` instead:

```bash
python -m tests.protocol.send_chunked localhost 8082 /chunked/echo
python -m tests.protocol.send_chunked localhost 8080 /chunked/echo   # through the gateway; use SITE_PORT
```

Options: `--text` (body to send, default `"chunked request body test"`), `--chunk-size`
(bytes per chunk, default `8`), `--chunk-delay` (seconds between chunks, default `0.2`).

For gRPC:

```bash
grpcurl -plaintext -d '{"name":"world"}' localhost:8083 echo.Echo/SayHello
```

## Verify through the gateway (expected to break)

1. In the emulator's `config.toml`, set:

   ```toml
   APP_UPSTREAM = http://localhost:8082
   ```

2. `SKIP_AUTH_ROUTES` takes regex patterns, not globs, and its value must be a quoted
   TOML string. Add this so you can test without a full OAuth login first:

   ```toml
   SKIP_AUTH_ROUTES = "/ws/,/sse/,/chunked/"
   ```

3. Start the emulator, then open `http://localhost:<SITE_PORT>/` (the gateway) instead
   of port 8082 directly, and repeat the same checks. Confirmed behavior (2026-07-07,
   this app vs. `src/app.py` on `main`):

   - **WebSocket** — the gateway responds `400 Bad Request` with a malformed response
     (headers from both the gateway and the upstream's `101` response are concatenated)
     instead of relaying the Upgrade handshake.
   - **SSE** — all events arrive at once, all within the same second, only after the
     full ~10-second upstream response finishes (`PROTOCOL_APP_SSE_COUNT` ×
     `PROTOCOL_APP_SSE_INTERVAL_SECONDS`, 10 × 1s by default) — `_proxy_to` reads the
     entire response body before writing anything back
     ([app.py:606](../../src/app.py#L606)). If the stream runs longer than the proxy's
     30s upstream read timeout ([app.py:603](../../src/app.py#L603)), the request fails
     with `502` instead.
   - **Chunked body** — `received_bytes` comes back as `0` (the gateway drops bodies sent
     with `Transfer-Encoding: chunked` and no `Content-Length`). Confirmed with both curl
     (sends the body as a single chunk) and `send_chunked.py` (multiple real chunks) —
     same result either way, since `_proxy_to` only ever checks for `Content-Length`
     ([app.py:598-599](../../src/app.py#L598-L599)) regardless of how many chunks made up
     the request.
   - **gRPC** — running the same `grpcurl`/gRPC client against the gateway's `SITE_PORT`
     instead of 8083 fails. The exact symptom depends on the client library, since both
     are surfacing the same root cause (the gateway is HTTP/1.1-only and cannot negotiate
     the HTTP/2 connection gRPC requires) differently:
     - Python's `grpc` package fails immediately: `grpc.RpcError: UNAVAILABLE — Failed
       parsing HTTP/2 (Expected SETTINGS frame as the first frame, ...)`.
     - Go's `grpcurl` instead hangs until its own dial deadline: `Failed to dial target
       host "localhost:<port>": context deadline exceeded`. Confirm the gateway itself is
       reachable first (e.g. `curl http://localhost:<SITE_PORT>/healthz` → `ok`) to rule
       out "nothing is listening" before treating this as the same gRPC gap.

     This is now the *default* behavior, not an unconditional gap: gRPC support is
     opt-in via `HTTP20_ENABLED`/`HTTP20_PROXY_MODE` (see the main README's
     ["HTTP/2 and gRPC"](../../README.md#http2-and-grpc) section). With those set, the
     same call succeeds — see `test_grpc_call_through_gateway` in
     `tests/python/test_protocol_gaps.py` for a full worked example (separate gateway
     instance, `HTTP20_ENABLED=true`/`HTTP20_PROXY_MODE=grpc-only`).

## Files

- `app.py` — the HTTP + gRPC verification servers
- `send_chunked.py` — sends a `Transfer-Encoding: chunked` POST split across multiple
  real chunks (see "Verify directly" above for usage)
- `send_http2.py` — sends one request over plaintext HTTP/2 (h2c) and prints the
  response; for testing `HTTP20_ENABLED` against ordinary (non-gRPC) routes, since
  Windows builds of curl typically have no HTTP/2 support at all (`curl --version` →
  no "HTTP2" in Features). For gRPC itself, use grpcurl or a real gRPC client instead.

  ```bash
  python -m tests.protocol.send_http2 localhost 8080 /healthz
  python -m tests.protocol.send_http2 localhost 8080 /.auth/login
  ```

- `echo.proto` — the minimal gRPC service definition
- `echo_pb2.py`, `echo_pb2_grpc.py` — generated from `echo.proto`; regenerate with:

  ```bash
  python -m grpc_tools.protoc -I tests/protocol --python_out=tests/protocol --grpc_python_out=tests/protocol tests/protocol/echo.proto
  ```

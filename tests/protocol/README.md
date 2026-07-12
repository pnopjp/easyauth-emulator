# Protocol gap verification app

Manual verification app for protocol behavior around WebSocket, gRPC, SSE/streaming, and
chunked request bodies, plus the same principal/claims/storage verification pages as
`src/sample_app.py` (both play the same demo-`APP_UPSTREAM` role — this is the "full", dev-only
edition, with gRPC added; `src/sample_app.py` is the "lite" edition shipped with the emulator's
own binary, without the extra `grpcio` dependency). Shared logic between the two lives in
`src/_sample_app_shared.py`. Standalone — not wired into `start.py` (unlike `src/sample_app.py`,
which `start.py` does launch automatically).

Reads `config.toml` the same way the emulator itself does, unless overridden with
`--config PATH` (used by `tests/python/test_protocol_gaps.py` to avoid touching a developer's
real, secret-bearing `config.toml` during automated tests):

```bash
python -m tests.protocol.app --config path/to/other-config.toml
```

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

- HTTP verification page: `http://localhost:8082/` (`SAMPLE_APP_PORT`, same setting as `src/sample_app.py`)
- gRPC service with reflection enabled: `localhost:8083` (`PROTOCOL_APP_GRPC_PORT`)

## Verify directly (expected to work)

Open `http://localhost:8082/` and try the WebSocket and SSE sections — both should work.
`http://localhost:8082/session` has the same principal/claims/Storage verification page as
`src/sample_app.py` (useful for checking Easy Auth header injection without needing a
separate app running).

The chunked-body section has no browser button: Chrome/Edge require HTTP/2 or HTTP/3 for
streaming `fetch` request bodies and throw `net::ERR_H2_OR_QUIC_REQUIRED` otherwise, and
browsers only ever negotiate HTTP/2 via TLS — never over plaintext, even though this app
also accepts plaintext HTTP/2 (h2c; see below). Use the curl command shown on the page
instead:

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

## Verify through the gateway

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
   of port 8082 directly, and repeat the same checks. Current behavior:

   - **WebSocket** — works correctly, from a client speaking either HTTP/1.1 (this app's
     page is loaded and tested that way by default) or HTTP/2 (RFC 8441 extended
     `CONNECT`). Either way, the gateway relays to `APP_UPSTREAM` as a classic `Upgrade`
     handshake, then shuttles raw bytes bidirectionally between the client and upstream
     until either side closes — not aware of WebSocket framing at all, so it works with
     any WebSocket application, not just this one. Real Azure App Service does the same
     conversion regardless of `HTTP20_PROXY_MODE` — see `tools/azure-poc/azure-websocket-poc`
     for details. This app doesn't implement RFC 8441 itself, matching real Azure App
     Service backends, which never need to either.
   - **SSE** — works correctly. Events are relayed as they arrive rather than being
     buffered until the upstream response completes (both `_proxy_to`'s HTTP/1.1 relay
     and `_http2_relay_request`'s HTTP/2 relay under `HTTP20_PROXY_MODE=all` support this).
   - **Chunked body** — works correctly. The `Transfer-Encoding: chunked` body is decoded
     and forwarded to `APP_UPSTREAM` as each chunk arrives, rather than being buffered in
     full first, and `received_bytes` matches the length of the sent body.
   - **HTTP/2 (`HTTP20_PROXY_MODE=all`)** — works correctly. This app also accepts
     plaintext HTTP/2 (h2c) alongside HTTP/1.1, so the gateway's genuine HTTP/2 relay
     under `all` (or `grpc-only` for non-gRPC content) no longer 502s against it.
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
     opt-in via `HTTP20_ENABLED`/`HTTP20_PROXY_MODE` (see the configuration reference's
     ["HTTP/2 and gRPC"](../../docs/configuration-reference.md#http2-and-grpc) section). With those set, the
     same call succeeds — see `test_grpc_call_through_gateway` in
     `tests/python/test_protocol_gaps.py` for a full worked example (separate gateway
     instance, `HTTP20_ENABLED=true`/`HTTP20_PROXY_MODE=grpc-only`).

     Two things to know when testing this with `grpcurl` specifically (through the
     gateway — either `SITE_PORT` or `APPSERVICE_HTTP20_ONLY_PORT`, not directly against
     port 8083):
     - **Unauthenticated calls get a fast `401`, not a hang.** An unauthenticated request
       to a protected route would normally redirect to `/.auth/login`, which a gRPC
       client can't follow — instead, requests with a `Content-Type` of `application/grpc*`
       get a plain `401` (+ `WWW-Authenticate: Bearer`, matching real App Service's
       dedicated gRPC port) so the client fails fast with `Unauthenticated` instead of
       hanging until its own call deadline.
     - **Server reflection works, so `-proto` is optional.** `grpcurl -plaintext
       localhost:<port> list` (no `-proto`) resolves the service list through the
       gateway, and `echo.Echo/SayHello` can be invoked without a local proto file too —
       both client-streaming and bidirectional-streaming RPCs (reflection included) are
       relayed correctly, since the gateway dispatches a request as soon as it starts
       rather than waiting for the client's stream to end. Real Azure App Service's
       gRPC listener does *not* support this once authenticated — server reflection
       itself fails with a platform-side error (see the "Result" section in
       `tools/azure-poc/azure-grpc-poc/README.md`) — and this gap is deliberately not
       reproduced here, since real gRPC clients normally use a compiled stub from a
       `.proto` file rather than depending on reflection at runtime; the divergence only
       matters for manual debugging tools like `grpcurl`.

## Files

- `app.py` — the HTTP + gRPC verification servers (imports `src/_sample_app_shared.py` for
  the principal/claims/storage pages and the WebSocket/SSE/chunked-body handlers, which it
  shares with `src/sample_app.py`)
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

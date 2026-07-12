# Azure gRPC + Easy Auth PoC

Empirically verifies an open question blocking the gRPC support design (see `ToDo.md`):
**does real Azure App Service Easy Auth protect the gRPC (`HTTP20_ONLY_PORT`) listener,
or does gRPC traffic bypass it entirely?** Official docs don't say either way — Easy Auth
docs don't mention gRPC, and the gRPC docs don't mention Easy Auth.

This app echoes back every piece of gRPC metadata it receives, so a real Easy
Auth-injected principal (if any) — or the raw cookie you send — will show up directly in
the response.

## Deploy

1. Create a Linux Web App (Python 3.12 runtime), any tier that supports custom startup
   commands (B1 or above).
2. Zip-deploy this folder (`app.py`, `requirements.txt`, `echo.proto`, `echo_pb2.py`,
   `echo_pb2_grpc.py`) or use `az webapp up` from this directory.
3. **Configuration → General settings**:
   - HTTP version: `2.0`
   - HTTP 2.0 Proxy: `gRPC only`
   - Startup Command: `python app.py`
4. **Configuration → Application settings**: add `HTTP20_ONLY_PORT` = `8585`
5. **Authentication** (Easy Auth): add an identity provider (Microsoft Entra ID Express
   setup is fastest). Note whatever "Action for unauthenticated requests" it's set to
   (usually "HTTP 302 Found redirect" by default).
6. Apply, and wait for the app to restart.

## Test 1 — does an unauthenticated gRPC call reach the app at all?

```bash
grpcurl -d '{"name":"world"}' <app-name>.azurewebsites.net:443 echo.Echo/SayHello
```

- **If it succeeds** (returns a `message`) → Easy Auth does not protect this port; gRPC
  traffic bypasses it entirely.
- **If it fails** (e.g. `PermissionDenied`, `Unauthenticated`, or a TLS/handshake error
  from Easy Auth intercepting it) → Easy Auth does intercept gRPC traffic; note the exact
  error, since that shapes how the emulator should reject/handle unauthenticated gRPC.

## Test 2 — does an authenticated call get principal metadata injected?

1. Open `https://<app-name>.azurewebsites.net/` in a browser and sign in via Easy Auth.
2. Open DevTools → Application/Storage → Cookies, and copy the value of
   `AppServiceAuthSession`.
3. Run:

   ```bash
   grpcurl -H "cookie: AppServiceAuthSession=<value>" -d '{"name":"world"}' <app-name>.azurewebsites.net:443 echo.Echo/SayHello
   ```

4. Inspect the returned `message` — it lists every metadata key/value the app received.
   Look for anything resembling `x-ms-client-principal`, `x-ms-client-principal-id`,
   `x-ms-client-principal-name`, etc. (the HTTP/1.1 equivalents this emulator already
   injects, see `src/app.py`).

## What the result decides

- **Bypasses Easy Auth entirely** → the emulator's gRPC listener can be a simple,
  auth-independent passthrough (Option A from the earlier discussion) — no principal
  metadata injection needed, matching real behavior.
- **Protected, with metadata injection** → the emulator needs a gRPC-aware proxy that
  authenticates the call and injects the equivalent metadata (Option B) — significantly
  more work, but a real gap otherwise.
- **Protected, but no metadata injection (just gated)** → a middle ground: check auth,
  reject if absent, but pure passthrough otherwise.

## Result (confirmed 2026-07-12)

| # | Question | Answer |
| --- | --- | --- |
| 1 | Does an unauthenticated gRPC call reach the app? | No — Easy Auth intercepts it and returns `401 Unauthorized` immediately (confirmed via `grpcurl`, no hang) |
| 2 | Does an authenticated call get principal metadata injected? | Yes — the exact same header set this emulator already injects on HTTP/1.1 (see `_AUTH_HEADERS_TO_STRIP` in `src/app.py`) shows up as gRPC metadata: `x-ms-client-principal-name`, `x-ms-client-principal-id`, `x-ms-client-principal-idp`, `x-ms-client-principal`, `x-ms-token-aad-access-token`, `x-ms-token-aad-id-token` |
| 3 | Does gRPC server reflection work over the authenticated real path? | No — with a valid `AppServiceAuthSession` cookie, `grpcurl` without `-proto` (which uses reflection, itself a bidirectional-streaming RPC) fails with `400`/`502` and the request never even reaches the app container (confirmed via container log tailing). Passing `-proto echo.proto` to bypass reflection works fine |

**Conclusion**: real Azure App Service Easy Auth **does** protect the gRPC (`HTTP20_ONLY_PORT`)
listener, and **does** inject the same principal/token metadata it injects as HTTP/1.1 headers
— this is "Option B" from the list above. Server reflection specifically seems to hit a real
platform-side limitation when authenticated, independent of the metadata-injection question;
this doesn't block using reflection for manual `grpcurl` exploration while unauthenticated (or
against the emulator itself, which handles reflection fine per
`tests/python/test_protocol_gaps.py::test_grpc_server_reflection_through_gateway`) but is worth
keeping in mind if a real client's discovery step depends on reflection specifically.

## Cleanup

The results above are already recorded in this README. Delete the Azure resource group
afterward to stop billing.

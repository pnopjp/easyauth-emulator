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

## Recording the result

Once you've run both tests, the answer decides the emulator's gRPC architecture:

- **Bypasses Easy Auth entirely** → the emulator's gRPC listener can be a simple,
  auth-independent passthrough (Option A from the earlier discussion) — no principal
  metadata injection needed, matching real behavior.
- **Protected, with metadata injection** → the emulator needs a gRPC-aware proxy that
  authenticates the call and injects the equivalent metadata (Option B) — significantly
  more work, but a real gap otherwise.
- **Protected, but no metadata injection (just gated)** → a middle ground: check auth,
  reject if absent, but pure passthrough otherwise.

Record the finding in this repo's memory / `ToDo.md` and delete the Azure resource group
afterward to stop billing.

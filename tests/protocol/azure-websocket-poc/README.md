# Azure WebSocket + HTTP/2 (RFC 8441) real-Azure verification PoC

Empirically verifies an open question blocking the RFC 8441 (WebSocket bootstrapping
over HTTP/2, extended `CONNECT` with a `:protocol` pseudo-header) item in `ToDo.md`:
**does real Azure App Service actually use RFC 8441 when both "HTTP version: 2.0" and
"Web sockets" are enabled, and if so, does it matter whether the backend app itself
speaks HTTP/1.1 or HTTP/2 (`http20ProxyFlag`)?** Official docs describe "HTTP version"
and "Web sockets" as unrelated settings with no mention of any interaction.

This uses a raw `h2`-library HTTP/2 client (not a browser) so the result doesn't
depend on any particular browser's RFC 8441 support or quirks.

## Result (confirmed 2026-07-10)

| # | Question | Answer |
| --- | --- | --- |
| 1 | Does App Service's front-end advertise `SETTINGS_ENABLE_CONNECT_PROTOCOL`? | Yes (`1`) |
| 2 | Does an RFC 8441 extended `CONNECT` to a WebSocket route succeed? | Yes (`:status 200`, full echo round-trip confirmed) |
| 3 | Does `http20ProxyFlag` (backend HTTP/1.1 vs HTTP/2) change this? | No |
| 4 | How is the request relayed to the backend? | **Always** as a classic HTTP/1.1 `Upgrade` handshake, regardless of `http20ProxyFlag` |
| 5 | What happens if the backend can only speak WebSocket over native HTTP/2 (no HTTP/1.1 Upgrade support)? | `502 Bad Gateway` — confirmed by deploying exactly that backend first |

**Conclusion**: Azure App Service's HTTP/2 front-end genuinely implements RFC 8441
toward the client, but internally *always* downgrades the WebSocket handshake to a
classic HTTP/1.1 `Upgrade` request before forwarding to the backend app — the backend
never needs to understand RFC 8441 itself, even when `http20ProxyFlag` makes it speak
HTTP/2 for everything else. This is a genuine fidelity gap versus this emulator, whose
`_Http2StreamHandler` currently returns `501` for the extended `CONNECT` (see `ToDo.md`
and `project_proxy_streaming_and_websocket.md` in this project's memory for the full
implication for the emulator's design).

This differs from Azure Container Apps, which has an unrelated, already-confirmed real
constraint (`ingress.transport: http2`/`auto` breaks WebSocket entirely, per
`microsoft/azure-container-apps` issues #280/#562) — Container Apps fidelity is
unaffected by this finding.

## Deploy

1. Create a Linux Web App (Python 3.12 runtime), any tier that supports custom startup
   commands and WebSockets (B1 or above — Free/Shared tiers don't support WebSockets).
2. Vendor `h2` and its dependencies into this folder (no build step needed at deploy
   time — Oryx's build pipeline does not reliably run `pip install` for a bare zip
   deploy in this configuration):

   ```bash
   pip install --target=./vendor h2==4.3.0 hpack hyperframe
   ```

3. Zip-deploy this folder (`app.py`, `vendor/`) — for example:

   ```bash
   az webapp deploy --resource-group <rg> --name <app-name> --src-path app.zip --type zip
   ```

   Build the zip with **forward-slash paths** (PowerShell's `Compress-Archive` writes
   backslash-separated paths on Windows, which breaks Linux-side `rsync` during
   deployment with `Invalid argument (22)` errors) — use Python's `zipfile` instead:

   ```python
   import zipfile, os
   with zipfile.ZipFile("app.zip", "w", zipfile.ZIP_DEFLATED) as zf:
       zf.write("app.py", "app.py")
       for root, dirs, files in os.walk("vendor"):
           for f in files:
               full = os.path.join(root, f)
               zf.write(full, os.path.relpath(full, ".").replace(os.sep, "/"))
   ```

4. **Configuration → General settings**:
   - HTTP version: `2.0`
   - Web sockets: `On`
   - Startup Command: `python app.py`
5. **Configuration → Application settings**: `WEBSITES_PORT` = `8000`,
   `SCM_DO_BUILD_DURING_DEPLOYMENT` = `false` (skip Oryx build entirely; the app has
   no build step since dependencies are vendored).

**Important**: leave `http20ProxyFlag` at its default (`0`, HTTP/1.1 to the backend)
for the initial deploy. Azure's own container **warmup/health probe always speaks
plain HTTP/1.1 to the backend, regardless of `http20ProxyFlag`** — `app.py` handles
both HTTP/1.1 and h2c on the same port precisely because a backend that only
understands h2c never passes this probe and the site never starts at all (confirmed
by deploying an h2c-only version first: `h2.exceptions.ProtocolError: Invalid HTTP/2
preamble` on every probe attempt, `ContainerTimeout` after 230s).

## Test 1 — does App Service use RFC 8441 toward the client at all?

```bash
python check_rfc8441.py <app-name>.azurewebsites.net
```

Expected output: ALPN negotiates `h2`, the server advertises
`SETTINGS_ENABLE_CONNECT_PROTOCOL = 1`, the extended `CONNECT` gets `:status 200`,
and the script prints the echoed WebSocket frame (`echo: hello`).

## Test 2 — does `http20ProxyFlag` (backend protocol) change the answer?

1. **Configuration → General settings → HTTP 2.0 Proxy**: set to `All` (or via CLI:
   `az webapp config set --generic-configurations '{"http20ProxyFlag": 1}'`).
2. Re-run `check_rfc8441.py`. If your backend implements *only* HTTP/1.1 Upgrade
   (like `app.py` here), you should see the same success as Test 1 — proving Azure
   downgrades the WebSocket handshake to HTTP/1.1 before forwarding, even when told
   to speak HTTP/2 to the backend for everything else.
3. To see what happens if the backend *can't* do the HTTP/1.1 Upgrade fallback,
   temporarily remove the `Upgrade: websocket` handling from `_handle_http11` in
   `app.py` and redeploy — the extended `CONNECT` will fail with `:status 502`
   (confirmed while developing this PoC).
4. Set `http20ProxyFlag` back to `0` afterward (`az webapp config set
   --generic-configurations '{"http20ProxyFlag": 0}'`).

## Recording the result

Record the finding in this repo's memory / `ToDo.md`, and delete the Azure resource
group afterward to stop billing (or just the Web App if the resource group is shared
with other things you want to keep).

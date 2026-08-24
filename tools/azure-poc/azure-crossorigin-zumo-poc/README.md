# App Service → Azure Functions cross-origin call demo (real-Azure PoC)

`../azure-zumo-auth-poc` (App Service) and `../azure-functions-zumo-auth-poc`
(Functions) each confirmed `X-ZUMO-AUTH`/client-directed sign-in independently. This
PoC wires the two together to exercise the real architecture pattern: a browser JS
client served from an Easy-Auth-protected App Service calling a *different-origin*
Functions API.

`app.py` is a minimal single-page HTML+JS server. The page's JS:

1. `GET /.auth/me` (same origin, cookie sent automatically) to read the
   `access_token` Easy Auth already put in this App Service's own token store.
2. POSTs that `access_token` to a **different-origin** Functions app's
   `POST /.auth/login/aad` (client-directed sign-in) to get that app's own
   `authenticationToken`.
3. Calls that Functions app's `GET /api/session` with
   `X-ZUMO-AUTH: <authenticationToken>` — a cookie can't cross origins, so this is
   the only option.

## Status: verification complete (C1-C6 confirmed — C4 is a new finding, C5 confirms the workaround and identity match, C6 confirms the same round trip against a local Emulator instance in the Functions role)

- **C1/C2 (confirmed, 2026-08-24)**: `az functionapp cors add` for the App Service's
  origin was enough — both `POST /.auth/login/aad` and `GET /api/session` succeeded
  cross-origin, including Easy Auth's own `/.auth/*` routes. No special handling
  needed beyond Functions' standard CORS setting.
- **C3 (confirmed, 2026-08-24)**: The full round trip (`/.auth/me` → the Functions
  app's `/.auth/login/aad` → `/api/session`) succeeded end to end in a real browser
  (every step `200`).
- **C4 (new finding, confirmed 2026-08-24)**: When Easy Auth builds the principal via
  the client-directed sign-in (`X-ZUMO-AUTH`) path, it does **not** surface whatever
  claims the access_token actually carries (`email`, `preferred_username`, `upn`,
  etc.) — verified even after adding those as optional claims for the access token in
  the app registration's Token configuration and confirming they were present in the
  decoded access_token. `X-MS-CLIENT-PRINCIPAL` (and `client_principal.claims`) only
  ever contains a fixed minimal set: `stable_sid`, `nameidentifier` (a synthesized
  `sid:...`), `identityprovider`, `ver`, `nbf`/`exp`/`iat`, `iss`/`aud`. This contrasts
  with the ordinary browser/cookie flow, where the id_token's `preferred_username`
  etc. do get reflected — this looks specific to the client-directed flow. (The
  initial hypothesis was "the access_token just doesn't carry profile claims" — real
  Azure testing showed that was wrong.)
- **C5 (confirmed, 2026-08-24)**: As a workaround for C4, `function_app.py` decodes
  the forwarded raw access_token itself (`X-MS-TOKEN-AAD-ACCESS-TOKEN`) and reports
  `oid`/`email`/`preferred_username` as `access_token_identity` in the `/api/session`
  response (implemented only in this PoC's own `function_app.py`, not in
  `_sample_app_shared.py`). Retrieved via the demo page's `X-ZUMO-AUTH` path, this
  matched the email of the actual user signed in on the App Service side —
  **directly confirming the same account is authenticated on both sides**, without
  needing to bypass Easy Auth's own (limited) principal.
- **C6 (confirmed, 2026-08-26)**: Ran the same full round trip with the Functions
  role played by a **local easyauth-emulator instance** (over HTTPS, using
  `site.localhost` with an mkcert certificate) instead of a real Azure Functions
  app — it also succeeded
  end to end (every step `200`). Both sides need to be HTTPS to avoid mixed-content
  blocking (the real App Service is `https://`; the first attempt used plain
  `http://localhost` and failed with `TypeError: Failed to fetch` from mixed
  content — switching to `site.localhost` with mkcert fixed it). This run also
  surfaced and fixed a real bug in the emulator's own CORS implementation:
  `Access-Control-Allow-Origin` was being sent twice on preflight responses
  (`end_headers()` already adds it via `_cors_response_headers()`; the preflight
  handler in `_dispatch()` was redundantly adding it again) — browsers reject a
  duplicated `Access-Control-Allow-Origin` outright.

## Open questions

| # | Question |
| --- | --- |
| C1 | Does a browser JS `POST <func-app>/.auth/login/aad` (cross-origin) succeed CORS-wise just by adding the App Service's origin to the Function App's CORS setting (Configuration → CORS), or does `/.auth/*` — handled by Easy Auth's own middleware, not user code — need something else? |
| C2 | Same question for `GET <func-app>/api/session`. |
| C3 | Does the full round trip (`/.auth/me` → the Functions app's `/.auth/login/aad` → `/api/session`) actually succeed end to end in a real browser? |

## Prerequisites

- The App Service from `../azure-zumo-auth-poc` (Easy Auth/Entra ID already configured).
- The Function App from `../azure-functions-zumo-auth-poc` (auth configured with the
  same AAD app registration, `/api/session` up and running).

## Deploy

Deploy this `app.py` to the existing App Service (overwriting whatever's deployed
there now). The disposable zip is created inside this PoC folder (already excluded
by `.gitignore`).

```powershell
$rg = "<resource-group-name>"
$appName = "<app-name>"  # the App Service used for azure-zumo-auth-poc / sample_app.py
$pocDir = "tools\azure-poc\azure-crossorigin-zumo-poc"

Compress-Archive -Path "$pocDir\app.py" -DestinationPath "$pocDir\crossorigin-app.zip" -Force
az webapp deploy --resource-group $rg --name $appName --src-path "$pocDir\crossorigin-app.zip" --type zip
az webapp config set --resource-group $rg --name $appName --startup-file "python app.py"
az webapp config appsettings set --resource-group $rg --name $appName --settings WEBSITES_PORT=8000
az webapp restart --resource-group $rg --name $appName
```

## Configure CORS on the Functions side

```powershell
$funcAppName = "<func-app-name>"

az functionapp cors add --resource-group $rg --name $funcAppName `
  --allowed-origins "https://$appName.azurewebsites.net"
```

## Test

1. Open `https://<app-name>.azurewebsites.net/` in a browser and sign in through
   Easy Auth.
2. Enter `https://<func-app-name>.azurewebsites.net` in the input field and click
   **Run**.
3. The page shows each step's HTTP status and result. Also check the browser
   devtools Network/Console tabs for CORS errors.

If C1/C2 fail with a CORS error, `az functionapp cors add` alone may not cover
`/.auth/*` routes. Check the exact CORS error message in devtools and work from
there.

## Recording results

Once run, fill in the **Status** section above with the actual answers and update
`ToDo.md` accordingly.

## Cleanup

Delete the App Service and Function App (or the whole resource group, if it's
dedicated to this PoC) afterward to stop billing.

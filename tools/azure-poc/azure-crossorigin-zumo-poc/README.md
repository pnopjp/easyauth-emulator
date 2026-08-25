# App Service → Azure Functions cross-origin call demo (real-Azure PoC)

`../azure-zumo-auth-poc` (App Service) and `../azure-functions-zumo-auth-poc`
(Functions) each confirmed `X-ZUMO-AUTH`/client-directed sign-in independently. This
PoC wires the two together to exercise the real architecture pattern: a browser JS
client served from an Easy-Auth-protected App Service calling a *different-origin*
Functions API.

`app.py` is a minimal single-page HTML+JS server. Both buttons start the same way —
`GET /.auth/me` (same origin, cookie sent automatically) to read the `access_token`
Easy Auth already put in this App Service's own token store — then diverge:

- **Run (X-ZUMO-AUTH)**: POSTs that `access_token` to a **different-origin**
  Functions app's `POST /.auth/login/<provider_name>` (client-directed sign-in —
  `<provider_name>` comes from `/.auth/me`'s own `provider_name` field, not
  hardcoded to `aad`) to get that app's own `authenticationToken`, then calls
  `GET /api/session` with `X-ZUMO-AUTH: <authenticationToken>` — a cookie can't
  cross origins, so this is the only option for that path.
- **Run (Authorization: Bearer)**: skips the `/.auth/login/<provider_name>` round
  trip entirely and presents the `access_token` directly to `GET /api/session` via
  a plain `Authorization: Bearer` header — the "daemon client application"
  (service-to-service) pattern from Microsoft's own docs.

## Status: verification complete (C1-C10 confirmed — C4 is a new finding, C5 confirms the workaround and identity match, C6/C7 confirm the same round trip with a local Emulator instance in either the Functions or App Service role, C8 confirms direct `Authorization: Bearer` also works, C9 confirms the same round trip also works with a non-AAD provider (Google), C10 confirms `Authorization: Bearer` direct validation is AAD-only and does not extend to other providers)

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
- **C7 (confirmed, 2026-08-26)**: The reverse of C6 — playing the **App Service role
  with a local easyauth-emulator instance** (HTTPS, `site.localhost`) instead, calling
  cross-origin into the existing real Functions app — also succeeded end to end
  (every step `200`). Along the way, the local emulator's `IDP_ENTRA_SCOPES` left at
  its default (`openid profile email` only) produced a Microsoft-Graph-audienced
  token (same `401` failure mode as before), and a leftover Storage-testing scope
  from unrelated prior work produced an `aud` of `https://storage.azure.com` at one
  point too. As on the App Service side (`loginParameters`), the local side also
  needs `IDP_ENTRA_SCOPES` explicitly set to
  `"openid profile email api://<client-id>/user_impersonation"` — commenting it out
  reverts to the Graph-audienced default rather than fixing anything.
- **C9 (confirmed, 2026-08-26)**: After generalizing `app.py` to stop hardcoding
  `/.auth/login/aad` and instead derive the path from `/.auth/me`'s own
  `provider_name`, switched the App Service role's IdP to Google and ran the same
  round trip — reaching success surfaced two new real-Azure findings along the way:
  1. Which field the client-directed login body needs differs by provider. AAD
     wants `access_token`; Google requires `id_token` instead (confirmed by the
     exact real error message, `'id_token' field is required.`). Fixed by having
     `app.py` send both `access_token` and `id_token` in the same body.
  2. `function_app.py`'s C5 workaround (`access_token_identity`) also broke for
     Google, since it only ever read the AAD-named header. Real Azure's Easy Auth
     names these headers per provider (e.g. `X-MS-TOKEN-GOOGLE-ACCESS-TOKEN`), so
     the handler now picks the header prefix from `X-MS-CLIENT-PRINCIPAL-IDP`
     (this repo's own Emulator, by contrast, always emits AAD-named headers
     regardless of the signed-in provider — a real difference from real Azure).

     After fixing both, Google succeeded end to end too (every step `200`,
     `access_token_identity` correctly showing the signed-in user's sub/email).
     Only AAD and Google are actually confirmed against real Azure — the other
     provider header-prefix guesses in `function_app.py` come from Microsoft's
     docs, not independent verification.
- **C10 (confirmed, 2026-08-26)**: While signed in via Google (App Service role),
  clicked **Run (Authorization: Bearer)** and got a CORS error whose underlying
  request was actually a `302` redirect to `login.windows.net` (AAD's own sign-in
  endpoint) — not a same-provider auth failure. This confirms that the
  "daemon client application" direct-`Authorization: Bearer` validation
  described in Microsoft's docs is **Azure AD (Microsoft Entra) only**: the
  ["Daemon client application (service-to-service calls)"](https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-aad#daemon-client-application-service-to-service-calls)
  section — "the resulting access token can then be presented to the target app
  via the standard OAuth 2.0 Authorization header. App Service authentication
  validates and uses the token" — exists only on the Microsoft Entra provider
  page; the [Google provider page](https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-google)
  has no equivalent section. Presenting a non-AAD provider's access_token via
  `Authorization: Bearer` isn't validated at all — Easy Auth falls through to
  its normal unauthenticated-request behavior (redirect to the configured
  default provider's sign-in), which is what the CORS error was actually
  masking. Client-directed sign-in (`X-ZUMO-AUTH`, C9) remains the
  provider-agnostic path; `Authorization: Bearer` (C8) is AAD-specific.

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
2. Enter `https://<func-app-name>.azurewebsites.net` in the input field.
3. Click **Run (X-ZUMO-AUTH)** (the path already confirmed in C1-C7). The page
   shows each step's HTTP status and result. Also check the browser devtools
   Network/Console tabs for CORS errors.
4. Also click **Run (Authorization: Bearer)** (C8 — presents the access_token
   directly to `/api/session` without the `/.auth/login/aad` round trip).

If C1/C2 fail with a CORS error, `az functionapp cors add` alone may not cover
`/.auth/*` routes. Check the exact CORS error message in devtools and work from
there.

### C8 (confirmed, 2026-08-26): direct call via Authorization: Bearer

Confirmed against real Azure: presenting `Authorization: Bearer <access_token>`
directly to a protected route — skipping `/.auth/login/<idp>` entirely — does
authenticate, exactly as Microsoft's own "daemon client application" docs
describe. Client-directed sign-in (`X-ZUMO-AUTH`) isn't required — a plain
standard OAuth 2.0 `Authorization` header works as an alternative, simpler path,
confirmed live.

## Recording results

Once run, fill in the **Status** section above with the actual answers and update
`ToDo.md` accordingly.

## Running locally

`app.py` itself is a minimal stdlib-only HTTP server with no dependencies — it runs
locally as-is. But it has no Easy Auth of its own, so — same reasoning as
`../azure-functions-zumo-auth-poc` — **this only becomes a meaningful test once
this repo's own Emulator sits in front as the "App Service role" gateway**,
injecting real Easy-Auth-shaped headers and serving `/.auth/me`/`/.auth/login/<idp>`.

### 1. Run app.py locally

```powershell
python tools\azure-poc\azure-crossorigin-zumo-poc\app.py
```

Listens on `http://localhost:8000` by default (override with the `PORT` env var).

### 2. Combine with the Emulator

In the Emulator's own `config.toml`, point `APP_UPSTREAM` at this local `app.py`:

```toml
APP_UPSTREAM = "http://localhost:8000"
```

Start (or restart) the Emulator, then open the Emulator's own
URL (e.g. `http://localhost:<SITE_PORT>/`) and sign in through Easy Auth — this
page now gets a real `access_token` from the Emulator's own token store.

From there, enter the Functions-role target's base URL and click **Run
(X-ZUMO-AUTH)** / **Run (Authorization: Bearer)** as usual. The target can be:

- A real Function App (as in C7), or
- `../azure-functions-zumo-auth-poc` run locally combined with its own Emulator
  instance (the reverse pairing from C6) — entirely local end to end, with no real
  Azure resources needed for this specific check.

## Cleanup

Delete the App Service and Function App (or the whole resource group, if it's
dedicated to this PoC) afterward to stop billing.

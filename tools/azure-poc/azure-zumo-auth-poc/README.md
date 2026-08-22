# Azure X-ZUMO-AUTH header real-Azure verification PoC

`X-ZUMO-AUTH` is not implemented anywhere in this emulator today. Before building
anything, we need to confirm real Azure App Service Easy Auth's actual behavior —
official docs describe it only in passing (as an alternative to the
`AppServiceAuthSession` cookie for non-browser clients) and don't specify the exact
mechanics.

This backend just echoes every header it received as JSON, so we can see precisely
what the App Service auth gatekeeper does (or doesn't) inject depending on how the
request was authenticated.

## Status: verification complete (Q1-Q6 confirmed, Q7 skipped)

- **Q1 (confirmed, 2026-08-21)**: `POST /.auth/login/aad`'s body needs an
  `access_token` field, not `id_token` (surfaced via `400 'access_token' field is
  required.`). On top of that, Easy Auth's `defaultAuthorizationPolicy.allowedApplications`
  defaults to just this app's own client ID, so an access_token whose `azp`/`appid`
  claim doesn't match (e.g. one obtained via `az` CLI or another client) is rejected
  with `403`. Using a client-credentials-flow access_token requested with **this
  app's own client ID/secret** (`azp` = itself) got `200 OK` with
  `{"authenticationToken": "<opaque, 472 chars>", "user": {"userId": "..."}}`.
- **Q2 (confirmed, 2026-08-21)**: A request to a protected route with only
  `X-ZUMO-AUTH: <authenticationToken>` (no `Cookie`) got `200 OK`, and the backend
  received `X-MS-CLIENT-PRINCIPAL-ID`, `X-MS-CLIENT-PRINCIPAL-IDP` (`aad`),
  `X-MS-CLIENT-PRINCIPAL`, and `X-MS-TOKEN-AAD-ACCESS-TOKEN` (no `X-Forwarded-User`/
  `X-MS-CLIENT-PRINCIPAL-NAME` since this was an app-only token with no user claims —
  not yet verified whether a delegated user token behaves the same).
- **Q3 (confirmed, 2026-08-21)**: `GET /.auth/me` also recognized `X-ZUMO-AUTH` alone
  and returned `200 OK` with the normal `/.auth/me` shape (array with `access_token`,
  `provider_name`, `user_claims`, `user_id`).
- **Q5 (confirmed, 2026-08-21)**: An invalid value (`X-ZUMO-AUTH: garbage`) got
  `401 Unauthorized` — unlike an invalid/missing cookie, which follows the configured
  `unauthenticatedClientAction` (`RedirectToLoginPage` here), an invalid `X-ZUMO-AUTH`
  appears to always return `401` directly rather than redirecting.
- **Q4 (confirmed, 2026-08-21)**: Pasting the `AppServiceAuthSession` cookie value
  (from a normal browser sign-in) verbatim into `X-ZUMO-AUTH` got `401 Unauthorized`
  (with a `WWW-Authenticate: Bearer ...` header). **`authenticationToken` and the
  session cookie value are not interchangeable** — they're distinct token formats.
- **Q6 (confirmed, 2026-08-21)**: Sending both a signed-in browser user's `Cookie` and
  an app-only `X-ZUMO-AUTH` (a different identity) on the same request produced the
  same `X-MS-CLIENT-PRINCIPAL-ID` as the `X-ZUMO-AUTH`-only test in Q2. **`X-ZUMO-AUTH`
  takes precedence over `Cookie` when both are present.**
- **Q7: skipped (2026-08-21)**. Since Q5 already showed an invalid `X-ZUMO-AUTH`
  returns `401` regardless of the configured `302` redirect default, switching that
  setting to `401` seems unlikely to change the outcome — but this wasn't actually
  verified.

## Open questions

| # | Question |
| --- | --- |
| Q1 | Does the client-directed flow (`POST /.auth/login/aad` with `{"access_token": "<AAD access_token>"}`) return a JSON body with `authenticationToken`? |
| Q2 | Does a request to a protected route with only `X-ZUMO-AUTH: <authenticationToken>` (no `Cookie`) get through, and does the backend receive the same `X-MS-CLIENT-PRINCIPAL*` headers as a normal cookie-based request? |
| Q3 | Does `GET /.auth/me` also recognize `X-ZUMO-AUTH` on its own (no cookie)? |
| Q4 | Is the `authenticationToken` format interchangeable with the `AppServiceAuthSession` cookie value — i.e. does pasting the cookie's value into `X-ZUMO-AUTH` also work? |
| Q5 | What happens with a garbage/expired `X-ZUMO-AUTH` value — same `302` redirect behavior as an invalid cookie, or a `401`/`403` JSON response? |
| Q6 | If both a valid `Cookie` and a valid `X-ZUMO-AUTH` (for a *different* identity) are sent together, which one wins? |
| Q7 | Does this differ between "Action to take when request is not authenticated" = `HTTP 302 redirect` vs `HTTP 401`? |

## Prerequisites

- An App Service (Linux, Python) with Easy Auth enabled, Microsoft/Entra ID as the
  identity provider (any existing test app registration works).
- A way to obtain a valid AAD **access_token** (not id_token) for that app
  registration outside of this PoC. If the app registration exposes an Application
  ID URI:

  ```powershell
  az login
  az account get-access-token --resource api://<client-id>
  ```

  Otherwise use MSAL/`az` device-code flow requesting a token for that client ID
  itself.

## Deploy

Reuses an existing Linux Web App that already has Easy Auth (Entra ID) configured —
no dependencies to vendor, so this is a plain single-file zip deploy:

```bash
az webapp deploy --resource-group <rg> --name <app-name> --src-path app.zip --type zip
az webapp config set --resource-group <rg> --name <app-name> --startup-file "python app.py"
az webapp config appsettings set --resource-group <rg> --name <app-name> --settings WEBSITES_PORT=8000
az webapp restart --resource-group <rg> --name <app-name>
```

## Test 0 — get a real AAD id_token (prerequisite for Q1/Q4/Q6)

Sign in normally through a browser:

```text
https://<app-name>.azurewebsites.net/.auth/login/aad
```

After completing sign-in, note the `AppServiceAuthSession` cookie value from the
browser's dev tools (used for Q4), and separately obtain an `id_token` for the same
app registration via MSAL or `az` device-code flow (used for Q1's request body).

## Test 1 — client-directed login (Q1)

```bash
curl -s -X POST https://<app-name>.azurewebsites.net/.auth/login/aad \
  -H "Content-Type: application/json" \
  -d '{"access_token": "<AAD_ACCESS_TOKEN>"}'
```

Record whether the response is JSON with an `authenticationToken` field, and what
its format looks like (opaque blob vs JWT-like).

## Test 2 — protected route with only X-ZUMO-AUTH (Q2, Q3)

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "X-ZUMO-AUTH: <authenticationToken from Test 1>"

curl -s -i https://<app-name>.azurewebsites.net/.auth/me \
  -H "X-ZUMO-AUTH: <authenticationToken from Test 1>"
```

Compare the echoed headers (`X-MS-CLIENT-PRINCIPAL*`, `X-MS-TOKEN-AAD-*`) against a
normal cookie-based request:

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "Cookie: AppServiceAuthSession=<cookie value from Test 0>"
```

## Test 3 — token interchangeability (Q4)

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "X-ZUMO-AUTH: <AppServiceAuthSession cookie value from Test 0>"
```

## Test 4 — invalid token behavior (Q5)

```bash
curl -s -i https://<app-name>.azurewebsites.net/ -H "X-ZUMO-AUTH: garbage"
```

## Test 5 — precedence when both are present (Q6)

Sign in as two different identities (or reuse the same one with a stale vs fresh
token) and send both headers on the same request; compare which identity's claims
show up in the echoed `X-MS-CLIENT-PRINCIPAL*` headers.

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "Cookie: AppServiceAuthSession=<identity A cookie>" \
  -H "X-ZUMO-AUTH: <identity B authenticationToken>"
```

## Addendum — comparing real Azure against the local implementation with `sample_app.py` (2026-08-21/22)

Q1-Q6 above were verified with this folder's minimal echo backend (`app.py`). Afterward,
`src/sample_app.py` (the emulator's own verification/demo app) was deployed to the same
App Service, and the resulting `X-ZUMO-AUTH` support implementation
(`_handle_client_directed_login`, `_check_auth_via_zumo`) was run locally against the
same AAD test app credentials, comparing `/api/session` output. Result: matching
(`X-MS-CLIENT-PRINCIPAL-ID`, `X-MS-CLIENT-PRINCIPAL-IDP: aad`, etc. were injected in the
same shape on both real Azure and the local emulator).

`sample_app.py` depends on the `h2` package via `src/_sample_app_shared.py`, so unlike
this folder's `app.py` it can't be zip-deployed with no build step. Package it straight
from `src/` when comparing against real Azure — don't keep a copy in this folder (`src/`
is the source of truth).

Disposable build artifacts (the staging folder and the zip) are created inside this
PoC folder, not the repo root — `.gitignore` already excludes
`tools/azure-poc/*/deploy-*/` and `tools/azure-poc/*/*.zip`.

```powershell
$rg = "<rg>"
$appName = "<app-name>"
$pocDir = "tools\azure-poc\azure-zumo-auth-poc"

New-Item -ItemType Directory -Force -Path "$pocDir\deploy-sample-app" | Out-Null
Copy-Item src\sample_app.py "$pocDir\deploy-sample-app\"
Copy-Item src\_sample_app_shared.py "$pocDir\deploy-sample-app\"
"h2==4.3.0" | Out-File -Encoding utf8 "$pocDir\deploy-sample-app\requirements.txt"

# Set the build-trigger setting BEFORE the first zip deploy (see gotchas below)
az webapp config appsettings set --resource-group $rg --name $appName `
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true SAMPLE_APP_PORT=8000

Compress-Archive -Path "$pocDir\deploy-sample-app\*" -DestinationPath "$pocDir\sample-app.zip" -Force
az webapp deploy --resource-group $rg --name $appName --src-path "$pocDir\sample-app.zip" --type zip
az webapp config set --resource-group $rg --name $appName --startup-file "python sample_app.py"
az webapp restart --resource-group $rg --name $appName
```

`deploy-sample-app/` and `sample-app.zip` are disposable local build artifacts — delete
them after deploying (though `.gitignore` keeps them out of `git status` either way).

### Gotchas

- **Set `SCM_DO_BUILD_DURING_DEPLOYMENT=true` before running `az webapp deploy`.**
  Setting it afterward doesn't retroactively rebuild anything already deployed — Oryx
  never installed `h2` from `requirements.txt`, so the container failed to start with
  `ModuleNotFoundError: No module named 'h2'`. Once set, redeploy the same zip.
- **`WEBSITES_PORT` didn't reliably take effect on the built-in Linux Python runtime**
  (not a custom container). Setting it to `8081` via both the CLI and the Portal still
  left the platform reporting `App port: 8000, Port selected by: Default for this
  image` (root cause not identified). Working around it by leaving `WEBSITES_PORT` at
  its default (`8000`) and instead pointing the app at that port
  (`SAMPLE_APP_PORT=8000`, since `sample_app.py` reads that env var for its listen
  port) was reliable.
- `az webapp config appsettings set`/`list` output shows every value as `null` on
  fairly recent az cli versions — that's CLI-side display masking, not proof the
  setting is actually empty. Check the Portal's Configuration blade to see real values.

## Recording results

Once run, fill in the **Status** section above with the actual answers and update
`ToDo.md` / this project's memory accordingly before any emulator-side
implementation work starts.

## Cleanup

Delete the Web App (or the whole resource group, if it's dedicated to this PoC)
afterward to stop billing.

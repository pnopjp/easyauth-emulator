# Azure Functions X-ZUMO-AUTH real-Azure verification PoC

`../azure-zumo-auth-poc` already confirmed `X-ZUMO-AUTH`/client-directed sign-in on
App Service (Q1-Q6). Microsoft's own [Japan PaaS Support Team
Blog](https://azure.github.io/jpazpaas/2023/10/23/access-to-easyauth-enabled-appservice-or-functions.html)
states the same flow applies to Azure Functions too, but the fine details
(`allowedApplications`, `403` behavior, etc.) haven't been verified against a real
Function App the way they were for App Service. This PoC closes that gap.

`function_app.py` is a minimal Azure Functions Python v2-model HTTP trigger
(`GET /api/session`) that calls `src/_sample_app_shared.py`'s `principal_summary()`
directly, so it returns the exact same JSON shape as App Service's `sample_app.py`
`/api/session` for a direct comparison. No rich HTML UI, WebSocket/SSE/HTTP2 demo —
out of scope for what this is actually verifying (header-injection parity).

`http_auth_level=ANONYMOUS` disables Functions' own key-based auth gate entirely, so
this isolates Easy Auth's own behavior from that unrelated mechanism.

## Status: in progress (F1, F2 confirmed)

- **F1 (confirmed, 2026-08-22)**: Sending a client-credentials access_token (issued for
  this app's own client ID/secret) to the Function App's `POST /.auth/login/aad` got
  `200 OK` with a JSON body containing `authenticationToken` — matches App Service (Q1).
- **F2 (confirmed, 2026-08-22)**: `GET /api/session` with only `X-ZUMO-AUTH` (no cookie)
  returned the same shape as App Service's `sample_app.py` `/api/session`, as expected.
- **F3 (confirmed, 2026-08-22)**: An invalid `X-ZUMO-AUTH: garbage` got `401` — matches
  App Service (Q5).

## Open questions

| # | Question |
| --- | --- |
| F1 | Does the client-directed flow verified for App Service (Q1: `POST /.auth/login/aad` with `{"access_token": "..."}`) return the same `{"authenticationToken": "...", "user": {"userId": "..."}}` shape on a Function App? |
| F2 | Does `GET /api/session` with only `X-ZUMO-AUTH` return the same shape/content (including `easyauth_headers` masking) as App Service's `sample_app.py` `/api/session`? |
| F3 | Does an invalid `X-ZUMO-AUTH` get the same `401` as App Service? |
| F4 | Separately from Functions' own `authLevel` (irrelevant here since it's `ANONYMOUS`), does Easy Auth's own `allowedApplications` policy behave the same as on App Service (only tokens for the same client ID accepted)? |

## Prerequisites

- The AAD app registration used in `../azure-zumo-auth-poc` (its client ID and tenant
  ID — referred to as `<client-id>`/`<tenant-id>` below) and its client-credentials
  access_token flow, reused here.

## Deploy

No Function App exists yet — create one in the existing resource group.

```powershell
$rg = "easyauth-emulator-test-rg"
$funcAppName = "<new-func-app-name>"
$storageAccount = "<new-storage-account-name>"  # globally unique, lowercase+digits
$location = "japaneast"

az storage account create --resource-group $rg --name $storageAccount --location $location --sku Standard_LRS

az functionapp create --resource-group $rg --name $funcAppName --storage-account $storageAccount `
  --consumption-plan-location $location --runtime python --runtime-version 3.12 `
  --functions-version 4 --os-type Linux
```

Deploy the code. `_sample_app_shared.py` isn't kept as a copy in this folder — it's
copied fresh from `src/` at build time each time (same reasoning as the App Service
`sample_app.py` deploy: `src/` is the source of truth).

Disposable build artifacts are created inside this PoC folder, not the repo root —
`.gitignore` already excludes `tools/azure-poc/*/deploy-*/` and `tools/azure-poc/*/*.zip`.

```powershell
$pocDir = "tools\azure-poc\azure-functions-zumo-auth-poc"

# Set the build-trigger setting BEFORE the first deploy (same gotcha as the App
# Service sample_app.py deploy — setting it afterward doesn't rebuild retroactively)
az functionapp config appsettings set --resource-group $rg --name $funcAppName `
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true

New-Item -ItemType Directory -Force -Path "$pocDir\deploy-func" | Out-Null
Copy-Item "$pocDir\function_app.py" "$pocDir\deploy-func\"
Copy-Item "$pocDir\host.json" "$pocDir\deploy-func\"
Copy-Item "$pocDir\requirements.txt" "$pocDir\deploy-func\"
Copy-Item src\_sample_app_shared.py "$pocDir\deploy-func\"

Compress-Archive -Path "$pocDir\deploy-func\*" -DestinationPath "$pocDir\func-app.zip" -Force
az functionapp deploy --resource-group $rg --name $funcAppName --src-path "$pocDir\func-app.zip" --type zip
```

`deploy-func/` and `func-app.zip` are disposable local build artifacts — delete them
after deploying (though `.gitignore` keeps them out of `git status` either way).

## Configure authentication

Azure Portal → this Function App → **Authentication** → **Add identity provider**

- Identity provider: **Microsoft**
- App registration: **Provide the details of an existing app registration** (don't
  create a new one)
  - Application (client) ID: `<client-id>`
  - Client secret: (same value used for the App Service PoC)
  - Issuer URL: `https://login.microsoftonline.com/<tenant-id>/v2.0`
- Additional checks: leave at defaults (fine this time — we're reusing a token whose
  `azp` already equals this same client ID, unlike the App Service PoC's first
  attempt with an az-cli-issued token from a different client)
- Authentication settings: leave at defaults, then Add

## Tests

### Getting an access_token (prerequisite for F1-F3)

Client-credentials flow using this app registration as its own client (`azp` ends up
equal to this same client ID, which satisfies the default `allowedApplications`
restriction directly). These tokens typically expire in about an hour — re-run this
if F1 starts returning a bare `401`.

Pasting the client secret into the Portal's identity-provider form also saves it as
this Function App's own `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` app setting, so it
can be fetched straight from this Function App — no dependency on the App Service PoC.

```powershell
$rg = "<resource-group-name>"
$funcAppName = "<func-app-name>"
$clientId = "<client-id>"    # same app registration as ../azure-zumo-auth-poc
$tenantId = "<tenant-id>"

$secret = az functionapp config appsettings list --resource-group $rg --name $funcAppName `
  --query "[?name=='MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'].value" -o tsv

$tokenResponse = Invoke-RestMethod -Method Post `
  -Uri "https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token" `
  -Body @{
    client_id     = $clientId
    client_secret = $secret
    scope         = "api://$clientId/.default"
    grant_type    = "client_credentials"
  }
$accessToken = $tokenResponse.access_token
```

### F1 — client-directed login

```powershell
$funcAppName = "<func-app-name>"

curl -i --show-error -X POST https://$funcAppName.azurewebsites.net/.auth/login/aad `
  -H "Content-Type: application/json" `
  -d "{`"access_token`": `"$accessToken`"}"
```

Check whether the response is JSON with an `authenticationToken` field.

### F2 — X-ZUMO-AUTH on /api/session

```powershell
$funcToken = "<authenticationToken from F1>"

curl -s https://$funcAppName.azurewebsites.net/api/session -H "X-ZUMO-AUTH: $funcToken"
```

Compare against App Service's `sample_app.py` `/api/session` output (from the real-Azure
run recorded in `../azure-zumo-auth-poc`, or the local emulator run).

### F3 — invalid token

```powershell
curl -s -i https://$funcAppName.azurewebsites.net/api/session -H "X-ZUMO-AUTH: garbage"
```

Check for `401`.

## Recording results

Once run, fill in the **Status** section above with the actual answers and update
`ToDo.md` accordingly.

## Cleanup

Delete the Function App and storage account (or the whole resource group, if it's
dedicated to this PoC) afterward to stop billing.

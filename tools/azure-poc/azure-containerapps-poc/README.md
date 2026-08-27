# Azure Container Apps Easy Auth PoC

`ToDo.md` has an open item to compare this emulator's behavior against real Container
Apps (as already done for App Service and Functions). Unlike those two, Container Apps
needs an actual container image rather than a zip deploy, and its built-in auth has no
"Express" one-click setup in the portal. This PoC deploys `src/sample_app.py` — the
same file shipped inside the emulator itself — into a real Container App with the
built-in auth (Easy Auth) feature enabled, to compare behavior directly.

Files here (`Dockerfile`, `__init__.py`, `_sample_app_shared.py`, `sample_app.py`) are
copies of `src/*`, since Container Apps builds from a container image rather than
running the repo directly. Re-copy them from `src/` if the sample app changes.

## Status: verification complete (Finding 1 is a real emulator divergence; Finding 2 confirms parity)

- **Finding 1 (confirmed 2026-08-25)**: `unauthenticatedClientAction=RedirectToLoginPage`
  only actually returns a `302` when the request's `User-Agent` header contains the
  substring `Mozilla`. Every other `User-Agent` — including curl's own default, no
  `User-Agent` at all, or an arbitrary non-browser string — gets a bare
  `401 Unauthorized` + `WWW-Authenticate: Bearer` instead, regardless of `Accept` or
  `Sec-Fetch-Mode`. Confirmed identical on the existing App Service PoC
  (`../azure-zumo-auth-poc`'s app, `easyauth-emulator-test-as.azurewebsites.net`), so
  this is a general Easy Auth platform behavior, not Container-Apps-specific. **The
  emulator diverges here**: `_deny_unauthenticated()` (`src/app.py:1312`) always
  redirects for non-gRPC requests regardless of `User-Agent`.
- **Token store (confirmed 2026-08-25)**: the managed-identity-based blob token store
  works. Reused the existing `easyauthemulatortestwcus` storage account (left over from
  `../azure-functions-zumo-auth-poc`) with a new `token-store` container, granted
  `Storage Blob Data Contributor` to the Container App's system-assigned identity
  (`az role assignment list --assignee <principalId>` confirms the assignment), then
  `az containerapp auth update --token-store true --blob-container-uri
  https://easyauthemulatortestwcus.blob.core.windows.net/token-store` succeeded with no
  SAS URL involved.
- **Finding 2 (confirmed 2026-08-27)**: an authenticated request's header injection is
  identical to App Service/Functions. `GET /api/session` with a real
  `AppServiceAuthSession` cookie returned `X-MS-CLIENT-PRINCIPAL-NAME`,
  `X-MS-CLIENT-PRINCIPAL` (masked), `X-MS-CLIENT-PRINCIPAL-ID` (masked),
  `X-MS-CLIENT-PRINCIPAL-IDP`, `X-MS-TOKEN-AAD-ACCESS-TOKEN` (masked), and
  `X-MS-TOKEN-AAD-ID-TOKEN` (masked) — the same set already confirmed for the gRPC port
  in `../azure-grpc-poc`. `X-Forwarded-User`/`X-Forwarded-Email` were absent, also
  matching prior findings. The ordinary browser/cookie flow (unlike the client-directed
  `X-ZUMO-AUTH` flow documented in `../azure-crossorigin-zumo-poc`'s C4) reflects the
  full real id_token claims — `email`, `preferred_username`, `groups`, `name` all showed
  up correctly for the signed-in account, with no divergence from the emulator's own
  claim shape.

## Deploy

```powershell
$rg = "easyauth-emulator-test-rg"
$location = "japaneast"
$acrName = "easyauthemulatortestacr"
$envName = "easyauth-emulator-test-env"
$contAppName = "easyauth-emulator-test-ca"
```

### 1. Build and push the image (remote build via ACR Tasks — no local Docker needed)

```powershell
az acr create --resource-group $rg --name $acrName --sku Basic --admin-enabled true
az acr build --registry $acrName --image easyauth-sample-app:latest tools/azure-poc/azure-containerapps-poc
```

### 2. Container Apps environment + the app itself

```powershell
az containerapp env create --resource-group $rg --name $envName --location $location

$acrPassword = az acr credential show -n $acrName --query "passwords[0].value" -o tsv
az containerapp create --resource-group $rg --name $contAppName `
  --environment $envName `
  --image "$acrName.azurecr.io/easyauth-sample-app:latest" `
  --target-port 8081 --ingress external `
  --registry-server "$acrName.azurecr.io" --registry-username $acrName --registry-password $acrPassword `
  --min-replicas 1 --max-replicas 1
```

This PoC's app is reachable at
`https://easyauth-emulator-test-ca.ambitiousglacier-64e6fc2c.japaneast.azurecontainerapps.io/`.

### 3. Authentication (Easy Auth)

Container Apps has no "Express setup" like App Service, so wiring up a provider takes
a few explicit steps. This PoC reuses the same AAD app registration
(`asami-easyauth-test`) already shared by `../azure-zumo-auth-poc`,
`../azure-functions-zumo-auth-poc`, and `../azure-crossorigin-zumo-poc`, instead of
creating a new one.

```powershell
# Add this Container App's callback URL to the existing app registration's redirect
# URIs (this call replaces the whole list, so pass every existing URI plus the new
# one — see `az ad app show --id <client-id>` first to get the current list).
az ad app update --id <client-id> --web-redirect-uris `
  <...existing URIs...> `
  "https://easyauth-emulator-test-ca.ambitiousglacier-64e6fc2c.japaneast.azurecontainerapps.io/.auth/login/aad/callback"
```

Adding the provider + client secret was done **via the portal** (Container App →
Authentication → Add identity provider → Microsoft → "Pick an existing app
registration" → select `asami-easyauth-test` → "Create a new client secret" →
unauthenticated action: `HTTP 302 Redirect`), because `az ad app credential reset`
(needed to mint the secret) was blocked by this session's own safety classifier as a
sensitive credential-issuing action. The equivalent CLI, if not blocked, is:

```powershell
az containerapp auth microsoft update -g $rg -n $contAppName `
  --client-id <client-id> --client-secret <secret> `
  --issuer "https://sts.windows.net/<tenant-id>/"
az containerapp auth update -g $rg -n $contAppName --enabled true --action RedirectToLoginPage
```

### 4. Token store (confirmed working — see actual values used below)

Container Apps' auth token store can persist tokens to a blob container, authenticating
via the Container App's own managed identity rather than a SAS URL:

```powershell
az containerapp identity assign --resource-group $rg --name $contAppName --system-assigned

# Reused the storage account already left over from ../azure-functions-zumo-auth-poc
# instead of creating a new one — any storage account works, a new container is enough.
$storageAccount = "easyauthemulatortestwcus"
$storageContainer = "token-store"
az storage container create --account-name $storageAccount --name $storageContainer --auth-mode login

# Grant the Container App's managed identity access to the container before enabling
# the token store, or the first write will fail with an authorization error.
$principalId = az containerapp show -g $rg -n $contAppName --query identity.principalId -o tsv
$storageId = az storage account show -g $rg -n $storageAccount --query id -o tsv
az role assignment create --assignee $principalId --role "Storage Blob Data Contributor" --scope $storageId

az containerapp auth update `
  --resource-group $rg `
  --name $contAppName `
  --token-store true `
  --blob-container-uri "https://$storageAccount.blob.core.windows.net/$storageContainer"
```

`--blob-container-uri` is a preview argument; omitting `--blob-container-identity`
defaults to the system-assigned identity assigned above.

## Cleanup

```powershell
az containerapp delete --resource-group $rg --name $contAppName --yes
az containerapp env delete --resource-group $rg --name $envName --yes
az acr delete --resource-group $rg --name $acrName --yes
```

Also delete the `token-store` container inside `easyauthemulatortestwcus` and the role
assignment granted to the Container App's identity — the storage account itself is
shared with `../azure-functions-zumo-auth-poc`, so don't delete the account. The client
secret added to `asami-easyauth-test` via the portal step is shared infrastructure
(reused across PoCs) — leave it unless it's confirmed unused elsewhere.

# EasyAuth Emulator

Develop locally with Azure App Service / Azure Functions / Azure Container Apps authentication — without Azure.

EasyAuth Emulator is a local authentication gateway that emulates Easy Auth behavior (multi-provider login, Easy Auth-compatible request headers, `/.auth/*` endpoints). This extension integrates it directly into your VS Code debug workflow: the emulator starts when you start debugging, and stops when you stop.

---

## Why?

Azure App Service, Azure Functions, and Azure Container Apps' built-in authentication feature (commonly known as Easy Auth) is powerful — but it only works inside Azure. This makes local development and testing difficult for applications that depend on Easy Auth headers and endpoints.

EasyAuth Emulator bridges that gap by running a compatible authentication gateway on your development machine, so you can develop and test as if Easy Auth were active.

---

## How it works

![How it works](https://raw.githubusercontent.com/pnopjp/easyauth-emulator/main/vscode-extension/images/flow.png)

The extension auto-detects your app's listening port from `launch.json`, framework config files (`.env`, `launchSettings.json`, `application.properties`, …), or debug output — so no manual wiring is needed in most projects.

---

## Features

- **Multi-IDP authentication** — Microsoft Entra ID, Google, GitHub, Apple, Facebook, and any OIDC-compatible provider
- **Easy Auth-compatible headers** — injects `X-MS-CLIENT-PRINCIPAL`, `X-MS-CLIENT-PRINCIPAL-ID`, and related headers into every authenticated request
- **Azure service compatibility** — Azure App Service, Azure Functions, Azure Container Apps, and Azure Static Web Apps (partial)
- **Auto-start / auto-stop** — emulator lifecycle is tied to your debug session
- **Smart port detection** — reads `launch.json`, framework configs, and debug stdout; prompts you only as a last resort
- **Secure credential storage** — client secrets are stored in the OS keychain, never in settings files
- **Custom OIDC providers** — add any OIDC-compatible provider via `easyauth.customIdps`
- **Status bar indicator** — click to interact with the emulator based on its current state (start, stop, or open logs)

---

## Requirements

| Requirement | Details |
| --- | --- |
| VS Code | 1.88 or later |
| Platform | Windows x64, macOS arm64, Linux x64 |

> **macOS x64 (Intel) and Linux arm64** — Pre-built binaries are not bundled for these platforms. Run `python scripts/package.py --vsix` from the repo root to build both the binary and the `.vsix`, then install it via **Extensions: Install from VSIX**.

No additional runtime installation is needed — the emulator binary is bundled inside the extension.

---

## Getting Started

### 1. Configure your identity provider

Open your workspace settings (`Ctrl+,` → search `easyauth`) and fill in the Client ID and Issuer URL for your IdP. See [Supported Identity Providers](#supported-identity-providers) for the required settings per provider.

### 2. Store the client secret

Run **EasyAuth Emulator: Set Client Secret** from the Command Palette (`Ctrl+Shift+P`) and enter the client secret for the IdP you configured in step 1. The secret is stored in VS Code's SecretStorage API (backed by the OS keychain — Windows Credential Manager on Windows) — never in settings files.

### 3. Register the callback URL

Add the following redirect URI in your identity provider's app registration:

```text
http://localhost:8080/oauth2/callback
```

If you changed `easyauth.site.port`, use that port instead.

---

## Usage

Press **F5** to start debugging. The emulator starts automatically after your app.

Once running, navigate to `http://localhost:8080/` in your browser. If you changed `easyauth.site.port`, use that port number instead. Use **EasyAuth Emulator: Open in Browser** from the Command Palette to open it directly.

---

## Status Bar

The status bar item in the bottom-left corner shows the emulator state at a glance. Clicking it performs a context-sensitive action:

| Display | Meaning | Click action |
| --- | --- | --- |
| `$(warning) EasyAuth: no config` | Not configured (no IdP set up) | Open Settings |
| `$(sync~spin) EasyAuth: starting...` | Emulator is starting | Open output |
| `$(shield) EasyAuth: 8080:3000` | Running — gateway port and upstream port | Open in browser |
| `$(shield) EasyAuth: stopped` | Stopped | Start emulator |
| `$(error) EasyAuth: error` (red background) | Failed to start | 1st click: open output / 2nd click: start emulator |

---

## Commands

All commands are available from the Command Palette (`Ctrl+Shift+P`):

| Command | Description |
| --- | --- |
| `EasyAuth Emulator: Start` | Start the emulator manually |
| `EasyAuth Emulator: Stop` | Stop the emulator |
| `EasyAuth Emulator: Restart` | Restart the emulator (picks up config changes) |
| `EasyAuth Emulator: Open Output` | Open the log output channel |
| `EasyAuth Emulator: Open in Browser` | Open the gateway URL in the browser |
| `EasyAuth Emulator: Set Client Secret` | Store a client secret in the OS keychain |
| `EasyAuth Emulator: Clear Client Secret` | Remove a stored client secret |

---

## Supported Identity Providers

### Built-in

| Provider | Required settings |
| --- | --- |
| **Microsoft Entra ID** | `easyauth.entra.clientId`, `easyauth.entra.oidcIssuerUrl` |
| **Google** | `easyauth.google.clientId` |
| **GitHub** | `easyauth.github.clientId` |
| **Apple** | `easyauth.apple.clientId` |
| **Facebook** | `easyauth.facebook.clientId` |

At least one `clientId` must be set for the emulator to start. All IdPs with a `clientId` configured are enabled automatically.

### Custom OIDC Providers

Add any OIDC-compatible provider via `easyauth.customIdps`:

```jsonc
// .vscode/settings.json
{
  "easyauth.customIdps": [
    {
      "name": "my-provider",
      "displayName": "My Provider",
      "clientId": "your-client-id",
      "oidcIssuerUrl": "https://your-provider.example.com"
    }
  ]
}
```

After adding a custom provider, store its client secret with **EasyAuth Emulator: Set Client Secret**.

Available fields per entry:

| Field | Required | Description |
| --- | :---: | --- |
| `name` | ✓ | IDP identifier used in `IDP_LIST` (lowercase alphanumeric and hyphens) |
| `clientId` | ✓ | OAuth2 / OIDC client ID |
| `oidcIssuerUrl` | ✓ | OIDC issuer URL (e.g. `https://your-provider.example.com`) |
| `displayName` | | Label shown on the IdP selection screen |
| `scopes` | | Space-separated OAuth2 scopes. Default: `openid profile email` |
| `authUserIdClaim` | | JWT claim used as user ID. Default: `sub` |
| `authProvider` | | Value set in `X-MS-CLIENT-PRINCIPAL-IDP` header |
| `prompt` | | OIDC `prompt` parameter (`login`, `consent`, etc.) |
| `codeChallengeMethod` | | PKCE code challenge method: `S256` or `plain` |
| `logoutEndpoint` | | Override the IdP logout URL |
| `skipClaimsFromProfileUrl` | | `true` to skip fetching claims from the userinfo endpoint |
| `extraArgs` | | Space-separated extra options passed to oauth2-proxy (e.g. `"--allowed-group=my-group --oidc-extra-audience=myapp"`) |
| `icon` | | Icon shown on the IdP selection page. Specify a [Simple Icons](https://simpleicons.org) slug (e.g. `"microsoft"`) or an image URL. Has no effect when `IDP_SELECT_ICONS` is `generic` or `text`. |

---

## Configuration Reference

### Extension behavior

| Setting | Default | Description |
| --- | --- | --- |
| `easyauth.autoStart` | `true` | Start emulator when a debug session begins |
| `easyauth.autoStop` | `true` | Stop emulator when the debug session ends |
| `easyauth.upstreamPort` | `null` | Fix the upstream port; `null` = auto-detect |
| `easyauth.portScanMax` | `5` | Ports to scan during auto-detection |
| `easyauth.portScanBase` | `null` | Base port for scanning; `null` = use first hint found |
| `easyauth.verbose` | `false` | Log all resolved configuration values on startup |

### Gateway

| Setting | Default | Description |
| --- | --- | --- |
| `easyauth.site.url` | `http://localhost` | Public base URL (used to build the OAuth2 callback URL) |
| `easyauth.site.port` | `8080` | Public port of the EasyAuth gateway |
| `easyauth.defaultIdp` | `""` | Default IdP when `/.auth/login` is accessed |
| `easyauth.skipAuthRoutes` | `""` | Routes that bypass auth — comma-separated `[METHOD=]REGEX` patterns |
| `easyauth.debugHeadersEndpointEnabled` | `false` | Enable `GET /.debug/headers` to inspect injected headers |
| `easyauth.idpSelectIcons` | `simple` | Icons on the IdP selection screen: `simple`, `generic`, or `text` |

### oauth2-proxy

| Setting | Default | Description |
| --- | --- | --- |
| `easyauth.oauth2proxy.portBase` | `4180` | Base port for internal oauth2-proxy instances |
| `easyauth.oauth2proxy.standardLogging` | `false` | Show startup/shutdown messages in the output channel |
| `easyauth.oauth2proxy.authLogging` | `false` | Show authentication events in the output channel |
| `easyauth.oauth2proxy.requestLogging` | `false` | Show per-request HTTP logs in the output channel |
| `easyauth.oauth2proxy.showDebugOnError` | `false` | Show detailed OIDC error info (useful during initial setup) |
| `easyauth.oauth2proxy.version` | `""` | Pin a specific oauth2-proxy version (e.g. `v7.6.0`) |
| `easyauth.oauth2proxy.autoUpdate` | `false` | Auto-update oauth2-proxy to the latest version on startup |
| `easyauth.oauth2proxy.sslCaBundle` | `""` | Path to a custom CA certificate bundle (PEM). Normally not needed — the OS certificate store is used automatically. |

---

## Implemented Easy Auth Endpoints

| Endpoint | Description |
| --- | --- |
| `GET /.auth/me` | Returns the current user's claims as JSON |
| `GET /.auth/login` | Redirects to the configured IdP login |
| `GET /.auth/login/select` | Shows the IdP selection screen (multi-IdP) — emulator only, not part of Azure Easy Auth |
| `GET /.auth/login/<idp>` | Logs in with the specified IdP |
| `GET /.auth/login/aad` | Alias for `entra` (Azure AD compatibility) |
| `GET /.auth/logout` | Logs out and clears the session |

---

## Easy Auth-compatible Headers

After authentication, the following headers are injected into upstream requests:

- `X-MS-CLIENT-PRINCIPAL` (Base64-encoded claims JSON)
- `X-MS-CLIENT-PRINCIPAL-ID`
- `X-MS-CLIENT-PRINCIPAL-IDP`
- `X-MS-CLIENT-PRINCIPAL-NAME`
- `X-MS-TOKEN-AAD-ACCESS-TOKEN`
- `X-MS-TOKEN-AAD-ID-TOKEN`
- `X-Forwarded-User`
- `X-Forwarded-Email`

Not yet implemented: `X-MS-TOKEN-AAD-EXPIRES-ON`, `X-MS-TOKEN-AAD-REFRESH-TOKEN`

---

## Troubleshooting

### Status bar shows `$(warning) EasyAuth: no config`

No IdP is configured. Set at least one `clientId` in your workspace settings, then run **EasyAuth Emulator: Set Client Secret** from the Command Palette.

### Emulator starts but one of my IdPs is not working

The client secret may be missing for that IdP. Check the **EasyAuth Emulator** output channel for a warning such as:

```text
[extension] Warning: entra clientId is set but no client secret found
```

Run **EasyAuth Emulator: Set Client Secret** and select the affected provider.

### Login fails — redirect URI mismatch (`AADSTS50011` or similar)

Register the following redirect URI in your IdP's app registration:

```text
http://localhost:8080/oauth2/callback
```

If you changed `easyauth.site.port`, use that port number instead.

### Login fails — `invalid_client`

The `clientId` or client secret does not match the IdP app registration. Double-check both values and run **EasyAuth Emulator: Set Client Secret** to update the secret.

### App port was not detected automatically

The extension could not determine which port your app listens on. Set `easyauth.upstreamPort` in your workspace settings:

```json
{ "easyauth.upstreamPort": 5000 }
```

### 502 error while debugging

The emulator cannot reach the upstream app. Possible causes:

- The app crashed or is still starting up and has not bound to its port yet
- If `easyauth.upstreamPort` is set manually, the port does not match the app's actual listen port

Check the app logs to confirm it is running correctly.

### Startup times out on the first run

On the first run, the emulator downloads `oauth2-proxy` from GitHub Releases, which may take more than 30 seconds. Watch the output channel for `oauth2-proxy ... installed at ...` to confirm the download is complete, then use **EasyAuth Emulator: Restart** to retry.

### Authentication callback shows a blank page

VS Code's built-in **Simple Browser** has limited cookie support and cannot complete OAuth2 flows. Use an external browser (Chrome, Edge, Firefox) instead.

### The emulator stopped when I started a second debug session

Only the first debug session controls the emulator. Stopping that session stops the emulator; subsequent sessions do not affect it. Use **EasyAuth Emulator: Start / Stop / Restart** from the Command Palette for manual control.

### The emulator process is still running after VS Code was force-killed

Internal `oauth2-proxy` processes are cleaned up automatically by the OS. If the emulator itself is orphaned, terminate it manually:

- **Windows:** End `easyauth-emulator.exe` in Task Manager
- **macOS:** Use Activity Monitor, or run `pkill easyauth-emulator`
- **Linux:** Run `pkill easyauth-emulator`

### More troubleshooting topics

For additional troubleshooting topics — including oauth2-proxy error diagnosis, OIDC configuration issues, and runtime diagnostics — see the [Runtime Guide: Troubleshooting](https://github.com/pnopjp/easyauth-emulator/blob/main/docs/configuration-reference.md#troubleshooting).

---

## Known Limitations

- `X-MS-TOKEN-AAD-EXPIRES-ON` and `X-MS-TOKEN-AAD-REFRESH-TOKEN` headers are not implemented
- Remote - SSH and Remote - Tunnels extensions have not been tested
- This is a development tool, not a byte-for-byte replica of Azure Easy Auth

---

## License

[Apache License 2.0](https://github.com/pnopjp/easyauth-emulator/blob/main/LICENSE)

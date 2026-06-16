# Runtime Guide

This document describes runtime configuration, compatibility boundaries, and troubleshooting information.

## Configuration

Security note:

- `config.toml` may contain secrets (for example `OAUTH2_PROXY_COOKIE_SECRET` and `IDP_<NAME>_CLIENT_SECRET`). Do not expose or commit `config.toml`.

### Global

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `IDP_LIST` | ✓ | — | Comma-separated IDP names to enable (e.g. `entra,google`). Order controls the selection page display order. |
| `DEFAULT_IDP` | | — | Default IDP when no session or selection cookie is present. Must be one of the `IDP_LIST` values. See default selection behavior below. |
| `SITE_URL` | | `http://localhost` | Public base URL of this gateway without a trailing slash. Used to construct the OAuth2 callback URL. |
| `SITE_PORT` | | `8080` | Public port of this gateway. Combined with `SITE_URL` to form the gateway base URL. |
| `APP_UPSTREAM` | | `http://localhost:8081` ※ | URL that authenticated requests are forwarded to. Set to your application's URL when using your own app. |
| `DEBUG_HEADERS_ENDPOINT_ENABLED` | | `false` | Enables the `GET /.debug/headers` diagnostic endpoint. When enabled, that URL shows the headers the emulator receives and computes. Disabled by default — returns `404`. |
| `SKIP_AUTH_ROUTES` | | — | Routes that bypass authentication and are forwarded directly to the upstream. Format: comma-separated list of `[METHOD=]REGEX` patterns matched against the request path. Example: `GET=^/health$,^/public/`. Injected auth headers are stripped before forwarding. |
| `IDP_SELECT_ICONS` | | `simple` | Icon style for the `/.auth/login/select` page. `simple` — Simple Icons CDN logo. `generic` — generic ID card icon (fully offline). `text` — no icon, text labels only. |
| `VERBOSE` | | `false` | Print all resolved configuration values on startup with secrets masked. Equivalent to passing `--verbose` / `-v` on the command line. |

※ If `SAMPLE_APP_PORT` is changed, the default becomes `http://localhost:<SAMPLE_APP_PORT>` when `APP_UPSTREAM` is not set.

Default selection behavior:

- If `DEFAULT_IDP` is set, `/.auth/login` redirects to that IDP.
- If `DEFAULT_IDP` is not set and `IDP_LIST` has exactly one item, that single IDP is treated as the default.
- If `DEFAULT_IDP` is not set and `IDP_LIST` has multiple items, `/.auth/login` shows the provider selection page.
- `IDP_LIST` order controls only the selection page list order.

### Per-IDP

Use `IDP_<NAME>_*` entries for each IDP listed in `IDP_LIST`.

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `IDP_<NAME>_DISPLAY_NAME` | | IDP name | Label shown on the IdP selection page. |
| `IDP_<NAME>_KIND` | | Inferred from IDP name | Identity provider backend type. Well-known names are auto-detected (`entra` → `microsoft`, etc.); others default to `openid-connect`. Accepted values: `microsoft` (Entra ID / Microsoft account), `google`, `apple`, `facebook`, `github`, `openid-connect`. |
| `IDP_<NAME>_CLIENT_ID` | ✓ | — | OAuth2 / OIDC client ID registered with the identity provider. |
| `IDP_<NAME>_CLIENT_SECRET` | ✓ | — | OAuth2 / OIDC client secret registered with the identity provider. |
| `IDP_<NAME>_OIDC_ISSUER_URL` | ✓ ※1 | ※2 | OIDC issuer URL. Required for `microsoft`, `google`, `apple`, and `openid-connect` KIND. |
| `IDP_<NAME>_AUTH_PROVIDER` | | Inferred from KIND | Value used as `identity_provider` in `/.auth/me` and `X-MS-CLIENT-PRINCIPAL-IDP` (e.g. `microsoft` → `aad`). |
| `IDP_<NAME>_AUTH_USER_ID_CLAIM` | | Inferred from KIND | JWT claim used as the user ID (e.g. `microsoft` → `preferred_username`, `google` → `email`). |
| `IDP_<NAME>_SCOPES` | | `openid profile email` | Space-separated OAuth2 scopes to request. Add extra scopes for delegated access scenarios. |
| `IDP_<NAME>_PROMPT` | | — | OIDC `prompt` parameter sent on every authorization request (`login`, `select_account`, `consent`). No effect for non-OIDC providers. |
| `IDP_<NAME>_LOGOUT_ENDPOINT` | | Derived from KIND | IdP logout URL. For `microsoft` KIND, auto-derived from the OIDC issuer URL. |
| `IDP_<NAME>_SKIP_CLAIMS_FROM_PROFILE_URL` | | `microsoft`: `true`, others: `false` | Whether oauth2-proxy skips fetching claims from the OIDC userinfo URL. Set to `true` to prevent userinfo from overwriting ID token claims. |

※1 Required only for `microsoft`, `google`, `apple`, and `openid-connect` KIND.

※2 Default values for `IDP_<NAME>_OIDC_ISSUER_URL` by KIND:

| KIND | Default |
| --- | --- |
| `microsoft` | `https://login.microsoftonline.com/common/v2.0` (use a tenant-specific URL for Entra ID) |
| `google` | `https://accounts.google.com` |
| `apple` | `https://appleid.apple.com` |
| `openid-connect` | — (required) |

### oauth2-proxy Settings

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `OAUTH2_PROXY_COOKIE_SECRET` | | Auto-generated | Secret used to sign oauth2-proxy session cookies. When not set, a secret is generated on first startup and appended to `config.toml`, so the same value is reused on subsequent restarts. |
| `OAUTH2_PROXY_COOKIE_SECURE` | | `false` | Sets the `Secure` flag on session cookies. Set to `true` for HTTPS deployments. |
| `OAUTH2_PROXY_PORT_BASE` | | `4180` | Base port for internal oauth2-proxy instances. Each IDP uses a consecutive port starting from this value (e.g. `4180`, `4181`, …). |
| `OAUTH2_PROXY_WHITELIST_DOMAIN` | | Derived from `SITE_URL`/`SITE_PORT` | Allowed domain for redirect targets. |
| `OAUTH2_PROXY_STANDARD_LOGGING` | | `false` | Show oauth2-proxy startup/shutdown messages in the terminal. |
| `OAUTH2_PROXY_AUTH_LOGGING` | | `false` | Show oauth2-proxy authentication event logs in the terminal. |
| `OAUTH2_PROXY_REQUEST_LOGGING` | | `false` | Show oauth2-proxy per-request HTTP logs in the terminal. |
| `OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR` | | `false` | Show debug information on OIDC errors (e.g. misconfigured client ID or issuer URL). Useful during development. Not recommended for production. |
| `OAUTH2_PROXY_PLATFORM` | | Auto-detected | Target platform for binary downloads. Only required when auto-detection fails. Accepted values: `windows-amd64`, `windows-arm64`, `linux-amd64`, `linux-arm64`, `linux-arm`, `darwin-amd64`, `darwin-arm64`. |
| `OAUTH2_PROXY_VERSION` | | latest | Version tag to download and maintain (e.g. `v7.6.0`). Defaults to the latest stable release (pre-releases excluded). No action if the installed version already matches. |
| `OAUTH2_PROXY_AUTO_UPDATE` | | `false` | Set to `true` to automatically update on every startup. When `false`, the check still runs and prints a notice if a newer version is available. Skipped if the network is unavailable. |

If `bin/oauth2-proxy/oauth2-proxy[.exe]` is absent on startup, it is downloaded automatically from GitHub Releases. When the binary is present, the latest version is always checked and a notice is printed if the installed version is not current.

Version management behavior:

| State | Action |
| --- | --- |
| Binary absent | Download (`OAUTH2_PROXY_VERSION` if set, otherwise latest) |
| Binary present, version pinned, mismatch | Update to the pinned version |
| Binary present, `AUTO_UPDATE = true` | Compare with latest (or pinned); update if different |
| Binary present, `AUTO_UPDATE = false` (default) | Check only; print a notice if a newer version is available |
| Network unavailable during version check | Skip check and continue startup |

### Network / SSL Settings

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `SSL_CA_BUNDLE` | | — | Path to a custom CA certificate bundle (PEM format). Normally not needed — the OS certificate store (Windows, macOS, Linux) is used automatically via [truststore](https://github.com/sethmlarson/truststore). Set this only when the required CA is not in the OS trust store, for example on Linux systems where the corporate CA cannot be added to the system store. |

### Verification App Settings

Settings for the optional verification app (`src/sample_app.py`). The app starts only when `SAMPLE_APP_ENABLED = true`.

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `SAMPLE_APP_ENABLED` | | `false` | Start sample_app.py as an internal verification app. |
| `SAMPLE_APP_PORT` | | `8081` | Internal port for sample_app.py. Set `APP_UPSTREAM` to this value to route requests through the sample app. |
| `SAMPLE_APP_STORAGE_BLOB_URL` | | — | Azure Blob Storage URL for delegated storage access verification. Format: `https://<account>.blob.core.windows.net/<container>/<blob>`. |
| `SAMPLE_APP_OBO_STORAGE_SCOPE` | | `https://storage.azure.com/.default` | OBO scope used when requesting a storage access token. |
| `SAMPLE_APP_STORAGE_TIMEOUT_SECONDS` | | `10` | Storage request timeout in seconds. |
| `SAMPLE_APP_STORAGE_PREVIEW_BYTES` | | `4096` | Number of bytes to preview from the storage response. |
| `SAMPLE_APP_TITLE` | | `Easy Auth verification app` | Title shown in the sample app UI. |
| `SAMPLE_APP_DESCRIPTION` | | — | Description shown in the sample app UI. |

## Troubleshooting

### Login fails with `invalid_client`

Verify `IDP_<NAME>_CLIENT_ID` and `IDP_<NAME>_CLIENT_SECRET` match your IdP app registration.

### Login fails after IdP redirect (`AADSTS50011`)

Redirect URI mismatch. Update the redirect URI in your IdP app registration (Authentication) to match:

```text
<SITE_URL>:<SITE_PORT>/oauth2/callback
```

For example: `http://localhost:8080/oauth2/callback`

### App not reachable (502 error)

Verify `APP_UPSTREAM` is set correctly and your application is running on that address.

### Inspecting forwarded headers

Set `DEBUG_HEADERS_ENDPOINT_ENABLED = true` in `config.toml`, then open `GET /.debug/headers` in a browser to see the headers the emulator is computing and forwarding.

### Viewing oauth2-proxy logs

Set one or more of `OAUTH2_PROXY_STANDARD_LOGGING`, `OAUTH2_PROXY_AUTH_LOGGING`, or `OAUTH2_PROXY_REQUEST_LOGGING` to `true` in `config.toml` to show the corresponding log category in the terminal.

For OIDC configuration errors, set `OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true` to see detailed diagnostic output.

Note: startup errors are always shown when oauth2-proxy exits unexpectedly, regardless of these settings.

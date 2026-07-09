# Runtime Guide

This document describes runtime configuration, compatibility boundaries, and troubleshooting information.

## Command-line Options

| Option | Default | Description |
| --- | --- | --- |
| `--app-upstream URL` | — | Override `APP_UPSTREAM`. Takes priority over `config.toml` and environment variables. Useful for changing the target port without editing the config file. |
| `--config PATH` | `config.toml` in current directory | Path to the configuration file. |
| `--verbose`, `-v` | `false` | Print all resolved configuration values on startup with secrets masked. Equivalent to `VERBOSE = true` in `config.toml`. |

## Configuration

Security note:

- `config.toml` may contain secrets (for example `OAUTH2_PROXY_COOKIE_SECRET` and `IDP_<NAME>_CLIENT_SECRET`). Do not expose or commit `config.toml`.

### Global

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `IDP_LIST` | ✓ | — | Comma-separated IDP names to enable (e.g. `entra,google`). Order controls the selection page display order. |
| `DEFAULT_IDP` | | — | Default IDP when no session or selection cookie is present. Must be one of the `IDP_LIST` values. See default selection behavior below. |
| `SITE_URL` | | `http://localhost` | Fallback base URL used when a request carries no `Host` header (without a trailing slash). Usually no change needed. Set an `https://` value only if a TLS-terminating front end (reverse proxy) does not send `X-Forwarded-Proto` — the dev tunnels edge sends it. |
| `SITE_PORT` | | `8080` | Listen port of this gateway (also the public port when accessed directly). |
| `APP_UPSTREAM` | | `http://localhost:8081` ※ | URL that authenticated requests are forwarded to. Set to your application's URL when using your own app. May be `https://` — see `SSL_CA_BUNDLE` for certificate verification. |
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

Use `IDP_<NAME>_*` entries for each IDP listed in `IDP_LIST`, where `<NAME>` is the IDP identifier from `IDP_LIST` converted to uppercase (e.g. `myoidc` in `IDP_LIST` → `IDP_MYOIDC_*` keys).

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `IDP_<NAME>_DISPLAY_NAME` | | IDP name | Label shown on the IdP selection page. |
| `IDP_<NAME>_ICON` | | — | Icon shown on the IdP selection page. Specify a [Simple Icons](https://simpleicons.org) slug (e.g. `microsoft`) or an image URL. Has no effect when `IDP_SELECT_ICONS` is `generic` or `text`. |
| `IDP_<NAME>_KIND` | | Inferred from IDP name | Identity provider backend type. Well-known names are auto-detected (`entra` → `microsoft`, etc.); others default to `oidc`. Accepted values: `microsoft` (Entra ID / Microsoft account), `google`, `apple`, `facebook`, `github`, `oidc` (alias: `openid-connect`). |
| `IDP_<NAME>_CLIENT_ID` | ✓ | — | OAuth2 / OIDC client ID registered with the identity provider. |
| `IDP_<NAME>_CLIENT_SECRET` | ✓ | — | OAuth2 / OIDC client secret registered with the identity provider. |
| `IDP_<NAME>_OIDC_ISSUER_URL` | ✓ ※1 | ※2 | OIDC issuer URL. Required for `microsoft`, `google`, `apple`, and `oidc` KIND. |
| `IDP_<NAME>_AUTH_PROVIDER` | | Inferred from KIND | Value used as `identity_provider` in `/.auth/me` and `X-MS-CLIENT-PRINCIPAL-IDP` (e.g. `microsoft` → `aad`). |
| `IDP_<NAME>_AUTH_USER_ID_CLAIM` | | Inferred from KIND | JWT claim used as the user ID (e.g. `microsoft` → `preferred_username`, `google` → `email`). |
| `IDP_<NAME>_SCOPES` | | `openid profile email` | Space-separated OAuth2 scopes to request. Add extra scopes for delegated access scenarios. |
| `IDP_<NAME>_PROMPT` | | — | OIDC `prompt` parameter sent on every authorization request (`login`, `select_account`, `consent`). No effect for non-OIDC providers. |
| `IDP_<NAME>_CODE_CHALLENGE_METHOD` | | `microsoft`/`google`/`apple`: `S256`, others: — | PKCE code challenge method (`S256` or `plain`). `microsoft`, `google`, and `apple` always use `S256` regardless of this setting. For `oidc` KIND, set to `S256` when the IdP supports it. No effect for non-OIDC providers. |
| `IDP_<NAME>_LOGOUT_ENDPOINT` | | Derived from KIND | IdP logout URL. For `microsoft` KIND, auto-derived from the OIDC issuer URL. |
| `IDP_<NAME>_SKIP_CLAIMS_FROM_PROFILE_URL` | | `microsoft`: `true`, others: `false` | Whether oauth2-proxy skips fetching claims from the OIDC userinfo URL. Set to `true` to prevent userinfo from overwriting ID token claims. |
| `IDP_<NAME>_EXTRA_ARGS` | | — | Space-separated extra options passed to oauth2-proxy for this IDP. Example: `"--allowed-group=my-group --oidc-extra-audience=myapp"`. See the [oauth2-proxy configuration reference](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview) for available options. |

※1 Required only for `microsoft`, `google`, `apple`, and `oidc` KIND.

※2 Default values for `IDP_<NAME>_OIDC_ISSUER_URL` by KIND:

| KIND | Default |
| --- | --- |
| `microsoft` | — (required) |
| `google` | `https://accounts.google.com` |
| `apple` | `https://appleid.apple.com` |
| `oidc` | — (required) |

### GitHub Provider Notes

oauth2-proxy's GitHub provider calls the GitHub `/user/emails` and `/user/orgs` APIs during session creation and requires the scopes `user:email` and `read:org`. The emulator sets these as the default scopes automatically.

**OAuth App:** Create the app under GitHub Settings → Developer settings → OAuth Apps. No additional configuration is needed beyond `IDP_GITHUB_CLIENT_ID` and `IDP_GITHUB_CLIENT_SECRET`.

**GitHub App:** When using a GitHub App instead of an OAuth App, the User Authorization (OAuth) flow uses the same `CLIENT_ID` / `CLIENT_SECRET` fields, but the GitHub App must have the following permission granted on the **Permissions & events** page:

| Section | Permission | Required level |
| --- | --- | --- |
| Account permissions | Email addresses | Read-only or Read and write |

Without this permission, login fails with a `500 Internal Server Error` in the browser. Enabling `OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true` reveals the underlying cause: `unexpected status "403": {"message":"Resource not accessible by integration"}`.

### Facebook Provider Notes

#### Email permission

oauth2-proxy's Facebook provider calls the Graph API (`/me?fields=name,email`) during session creation and requires the `email` field. The emulator sets `public_profile email` as the default scopes automatically, but the `email` permission must be explicitly added to your app. Go to **App Dashboard → Permissions and Features**, find `email`, and click **Add**. Without this step, the OAuth flow is interrupted mid-login with a Facebook error page that includes the message `Invalid Scopes: email`. Enabling `OAUTH2_PROXY_REQUEST_LOGGING = true` reveals `error_code=100` in the logged callback URL.

#### HTTPS required

Facebook Login requires the redirect URI to use HTTPS. Configure `TLS_CERT_FILE` and `TLS_KEY_FILE` and set `SITE_URL` to an `https://` URL before testing. For local development, setting `SITE_URL` to `https://site.localhost` and registering `https://site.localhost:<port>/oauth2/callback` in the Facebook app's valid OAuth redirect URIs works well. A certificate for `site.localhost` can be generated with mkcert (see [Enabling HTTPS](#enabling-https-tls) below).

### oauth2-proxy Settings

| Parameter | Required | Default | Description |
| --- | :---: | --- | --- |
| `OAUTH2_PROXY_COOKIE_SECRET` | | Auto-generated | Secret used to sign oauth2-proxy session cookies. When not set, a secret is generated on first startup and appended to `config.toml`, so the same value is reused on subsequent restarts. |
| `OAUTH2_PROXY_COOKIE_SECURE` | | `false` | Sets the `Secure` flag on session cookies. Automatically set to `true` when HTTPS is enabled via `TLS_CERT_FILE`/`TLS_KEY_FILE`. |
| `OAUTH2_PROXY_PORT_BASE` | | `4180` | Base port for internal oauth2-proxy instances. Each IDP uses a consecutive port starting from this value (e.g. `4180`, `4181`, …). |
| `OAUTH2_PROXY_WHITELIST_DOMAIN` | | Derived from `SITE_URL`/`SITE_PORT` | Allowed domain for redirect targets. |
| `OAUTH2_PROXY_TRUSTED_PROXY_IP` | | `127.0.0.1,::1` when `APP_UPSTREAM` is localhost | Comma-separated list of trusted reverse-proxy IP addresses or CIDRs from which `X-Forwarded-*` headers are accepted. Auto-set to `127.0.0.1,::1` when `APP_UPSTREAM` points to `localhost`, `127.0.0.1`, or `[::1]`. Set explicitly for non-local setups such as Docker (e.g. `172.17.0.0/16`). |
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
| `TLS_CERT_FILE` | | — | Path to the TLS server certificate (PEM format). When set together with `TLS_KEY_FILE`, the emulator accepts inbound requests over HTTPS. |
| `TLS_KEY_FILE` | | — | Path to the TLS private key (PEM format). When set together with `TLS_CERT_FILE`, the emulator accepts inbound requests over HTTPS. |
| `SSL_CA_BUNDLE` | | — | Path to a custom CA certificate bundle (PEM format). Used for outbound HTTPS connections the emulator makes: to GitHub (oauth2-proxy downloads), and to `APP_UPSTREAM` when it's `https://`. Normally not needed — the OS certificate store (Windows, macOS, Linux) is used automatically via [truststore](https://github.com/sethmlarson/truststore) (a locally-issued dev cert, e.g. mkcert, verifies once its CA is installed into that store). Set this only when your network has SSL inspection (MITM proxy) and the proxy CA cannot be added to the OS trust store, for example on Linux without root access. |
| `HTTP20_ENABLED` | | `false` | Accept HTTP/2 on `SITE_PORT`, alongside HTTP/1.1 (additive, not exclusive — mirrors Azure App Service's "HTTP version: 2.0"). See [HTTP/2 and gRPC](#http2-and-grpc) below. |
| `HTTP20_PROXY_MODE` | | `disabled` | How much of an HTTP/2 request is preserved end to end to `APP_UPSTREAM` (mirrors Azure App Service's `http20ProxyFlag`): `disabled` downgrades everything to HTTP/1.1 (breaks gRPC); `all` relays everything as HTTP/2 (`APP_UPSTREAM` must speak HTTP/2); `grpc-only` relays only requests with a `Content-Type` of `application/grpc*`. Only takes effect while `HTTP20_ENABLED` is on — with it off, `SITE_PORT` never receives an HTTP/2 request to preserve, so everything is relayed as HTTP/1.1 regardless of this setting (`APPSERVICE_HTTP20_ONLY_PORT` below is the one exception). |
| `APPSERVICE_HTTP20_ONLY_PORT` | | — | Dedicated HTTP/2-only port for gRPC (mirrors Azure App Service's `HTTP20_ONLY_PORT` app setting; Azure Container Apps has no equivalent concept, so leave this unset for that case). When set, this port always listens as HTTP/2 and always relays to `APP_UPSTREAM` as HTTP/2, regardless of `HTTP20_ENABLED`/`HTTP20_PROXY_MODE`. Must differ from `SITE_PORT`. See [HTTP/2 and gRPC](#http2-and-grpc) below. |

#### Enabling HTTPS (TLS)

Set `TLS_CERT_FILE` and `TLS_KEY_FILE` to make the gateway listen on HTTPS. Using `site.localhost` as the host is recommended (required for Facebook Login).

Modern browsers resolve `*.localhost` to `127.0.0.1` automatically (RFC 6761), so no hosts file entry is needed for browser access. Non-browser HTTP clients may require one:

```text
# C:\Windows\System32\drivers\etc\hosts (Windows) or /etc/hosts (macOS/Linux)
127.0.0.1  site.localhost
```

Update `config.toml`:

```toml
SITE_URL      = "https://site.localhost"
SITE_PORT     = "8443"
TLS_CERT_FILE = "./server.crt"
TLS_KEY_FILE  = "./server.key"
```

> Update the redirect URI in your IdP app registration to `https://site.localhost:8443/oauth2/callback`.

`OAUTH2_PROXY_COOKIE_SECURE` is automatically set to `true` when TLS is enabled and the option is not explicitly configured.

##### Recommended: generate a certificate with mkcert

[mkcert](https://github.com/FiloSottile/mkcert) generates locally-trusted development certificates by registering a CA in the OS certificate store. No browser warnings.

Available at: [https://github.com/FiloSottile/mkcert](https://github.com/FiloSottile/mkcert)

```sh
mkcert -install  # register the CA (first time only)
mkcert -cert-file server.crt -key-file server.key site.localhost
```

Place the generated `server.crt` and `server.key` at the paths specified in `config.toml`.

##### Alternative: self-signed certificate with openssl

```sh
openssl req -x509 -newkey rsa:4096 -keyout server.key -out server.crt \
  -sha256 -days 365 -nodes -subj "/CN=site.localhost"
```

Self-signed certificates cause a browser security warning.

#### HTTP/2 and gRPC

By default the gateway speaks HTTP/1.1 only, on the same `SITE_PORT` used for everything
else. The HTTP/2 support added by `HTTP20_ENABLED` and `HTTP20_PROXY_MODE` (see the table
above) is additive to HTTP/1.1 (not exclusive of it) — mirroring how Azure App Service's own
"HTTP version: 2.0" setting works:

```toml
HTTP20_ENABLED = true            # accept HTTP/2 on SITE_PORT, alongside HTTP/1.1
HTTP20_PROXY_MODE = "grpc-only"  # "disabled" | "all" | "grpc-only"
```

`HTTP20_PROXY_MODE` controls how much of an HTTP/2 request is preserved end to end to
`APP_UPSTREAM`, mirroring App Service's `http20ProxyFlag`:

- **`disabled`** (default) — every request to `APP_UPSTREAM` is sent as HTTP/1.1, even if
  the client used HTTP/2. Fine for ordinary request/response traffic; breaks gRPC, whose
  streaming and trailer-based status (`grpc-status`) cannot be represented over HTTP/1.1.
- **`all`** — every request is relayed to `APP_UPSTREAM` as HTTP/2. Requires `APP_UPSTREAM`
  to actually speak HTTP/2 on that port (plaintext h2c, or TLS with ALPN — e.g. nginx, which
  only ever speaks HTTP/2 over TLS, works when `APP_UPSTREAM` is `https://`).
- **`grpc-only`** — only requests whose `Content-Type` starts with `application/grpc` are
  relayed as HTTP/2; everything else is downgraded to HTTP/1.1 as in `disabled`. Use this
  when `APP_UPSTREAM` serves both a gRPC service and ordinary HTTP endpoints on the same
  port.

Notes:

- Client-facing HTTP/2 accepts both plaintext (h2c) and, when `TLS_CERT_FILE`/`TLS_KEY_FILE`
  are set, TLS with ALPN negotiation — matching how real browsers speak HTTP/2 (TLS-only).
- `APP_UPSTREAM` may be `https://`, for both `HTTP20_PROXY_MODE = "disabled"` (relayed as
  HTTP/1.1 over TLS) and `"all"`/`"grpc-only"` (relayed as HTTP/2 over TLS with ALPN,
  failing if the upstream doesn't negotiate `h2`). Certificate verification uses
  `SSL_CA_BUNDLE` if set, otherwise the OS trust store — so a locally-issued dev cert (e.g.
  mkcert) verifies once its CA is installed into the OS store, with no extra configuration.
- By default, gRPC shares `SITE_PORT` with everything else, matching Azure Container Apps'
  single-ingress model. To emulate App Service's separate port instead, set
  `APPSERVICE_HTTP20_ONLY_PORT` (see "Configuring for Azure App Service" below).
- The relay to `APP_UPSTREAM` over HTTP/2 is unary request/response only (matching
  `_proxy_to`'s existing HTTP/1.1 behavior, which also fully buffers both directions) — not
  a general bidirectional-streaming gRPC client.

##### Configuring for Azure Container Apps

Azure Container Apps ingress uses a single `transport` setting (`auto` / `http` / `http2` /
`tcp`) that decides both the client-facing and upstream-facing protocol at once. This
emulator splits that into two settings instead, so here's how `transport` values map onto
them:

| Container Apps `ingress.transport` | Emulator settings |
| --- | --- |
| `http` (HTTP/1.1 only) | `HTTP20_ENABLED = false` |
| `http2` (HTTP/2 only, every request) | `HTTP20_ENABLED = true`, `HTTP20_PROXY_MODE = "all"` |
| `auto` (negotiated per connection; typical when an app serves gRPC alongside ordinary HTTP) | `HTTP20_ENABLED = true`, `HTTP20_PROXY_MODE = "grpc-only"` |

Also note gRPC already shares `SITE_PORT` with everything else here, same as Container
Apps' single-ingress model. No separate port to configure, unlike App Service.

##### Configuring for Azure App Service

App Service requires a separate port for gRPC (the `HTTP20_ONLY_PORT` app setting),
distinct from `SITE_PORT`. To emulate this, set `APPSERVICE_HTTP20_ONLY_PORT` (which must
differ from `SITE_PORT`):

```toml
SITE_PORT = 8080
APPSERVICE_HTTP20_ONLY_PORT = 8082
```

Once set:

- This port always listens as HTTP/2, regardless of `HTTP20_ENABLED` (h2c, or TLS with ALPN
  restricted to `h2` when `TLS_CERT_FILE`/`TLS_KEY_FILE` are set).
- Requests to this port are always relayed to `APP_UPSTREAM` as HTTP/2, regardless of
  `HTTP20_PROXY_MODE`.

With `APPSERVICE_HTTP20_ONLY_PORT` set and `HTTP20_PROXY_MODE = "grpc-only"`, `SITE_PORT`'s
own behavior changes too: real App Service's port 443 still handles ordinary HTTP/2 traffic,
but gRPC is only ever reachable through the dedicated port, so `SITE_PORT` stops
special-casing `Content-Type` for gRPC, and relays everything to `APP_UPSTREAM` as HTTP/1.1
(the same as `disabled`). Send gRPC calls to `APPSERVICE_HTTP20_ONLY_PORT` instead. With
`HTTP20_PROXY_MODE` set to `all` or `disabled`, `SITE_PORT`'s behavior is unchanged.

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

Redirect URI mismatch. The callback URL follows the origin shown in the browser's address bar. Update the redirect URI in your IdP app registration (Authentication) to match:

```text
<origin the browser uses>/oauth2/callback
```

For example: `http://localhost:8080/oauth2/callback`, or `https://xxx-8080.usw2.devtunnels.ms/oauth2/callback` when accessing through a forwarded domain. Register one entry per origin you use.

### App not reachable (502 error)

Several possible causes are listed below.

#### 1. `APP_UPSTREAM` misconfigured or not running

Verify `APP_UPSTREAM` is set correctly and your application is running on that address.

#### 2. Upstream HTTP/2 header size limit (`HTTP20_PROXY_MODE = "all"` / `"grpc-only"`)

If this only happens after a successful login (not on every request), the upstream may be
rejecting the request over HTTP/2 header size limits: oauth2-proxy's session cookie can
grow past several KB once it embeds the access/ID tokens
(`--pass-access-token`/`--set-authorization-header`), and servers like nginx cap HTTP/2
request header sizes by default. Check the emulator's own output for a line like:

```text
[app] upstream HTTP/2 relay to <host>:<port> (tls=True) failed: ConnectionError('no HTTP/2 response received from ... (upstream reset the stream (error_code=<ErrorCodes.ENHANCE_YOUR_CALM: 11>))')
```

If the server software behind `APP_UPSTREAM` is nginx, raise the relevant limit and
reload: `http2_max_field_size` /
`http2_max_header_size` (nginx before 1.19.7) or `large_client_header_buffers` (1.19.7
onward, shared with HTTP/1.x). Otherwise, switch to `HTTP20_PROXY_MODE = "disabled"` (or
`"grpc-only"` if only specific gRPC routes need HTTP/2) if the upstream doesn't actually
need the rest of the request relayed as HTTP/2.

### oauth2-proxy returns HTTP 500

Several possible causes are listed below.

#### 1. Client secret is incorrect

Ensure `IDP_<NAME>_CLIENT_SECRET` is the **secret value**, not the secret ID (object ID).

To diagnose, enable one of the following:

- **`OAUTH2_PROXY_STANDARD_LOGGING = true`**: The following message appears in the output:

  ```text
  [oauthproxy.go:928] Error redeeming code during OAuth2 callback: token exchange failed: oauth2: "invalid_client" "AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID, for a secret added to app '<app-id>'."
  ```

- **`OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true`**: The browser shows a 500 error page with the following detail:

  ```text
  500
  Internal Server Error

  token exchange failed: oauth2: "invalid_client" "AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID, for a secret added to app '<app-id>'."
  ```

### Inspecting forwarded headers

Set `DEBUG_HEADERS_ENDPOINT_ENABLED = true` in `config.toml`, then open `GET /.debug/headers` in a browser to see the headers the emulator is computing and forwarding.

### Viewing oauth2-proxy logs

Set one or more of `OAUTH2_PROXY_STANDARD_LOGGING`, `OAUTH2_PROXY_AUTH_LOGGING`, or `OAUTH2_PROXY_REQUEST_LOGGING` to `true` in `config.toml` to show the corresponding log category in the terminal.

For OIDC configuration errors, set `OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true` to see detailed diagnostic output.

Note: startup errors are always shown when oauth2-proxy exits unexpectedly, regardless of these settings.

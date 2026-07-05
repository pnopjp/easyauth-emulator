#!/usr/bin/env python3
import base64
import datetime
import html
import http.client
import json
import os
import re
import ssl
import sys
import tomllib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse, urlsplit
from urllib.request import Request as UrlRequest, urlopen

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_config(config_path: "Path | None" = None) -> dict[str, str]:
    if config_path is None:
        config_path = Path.cwd() / "config.toml"
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    result: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, bool):
            result[k] = "true" if v else "false"
        elif isinstance(v, list):
            result[k] = ",".join(str(item) for item in v)
        else:
            result[k] = str(v)
    return result


def _parse_args() -> "Path | None":
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=str, default=None)
    known, _ = parser.parse_known_args()
    return Path(known.config) if known.config else None


_CONFIG = _load_config(_parse_args())


def _cfg(key: str, default: str = "") -> str:
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    return _CONFIG.get(key, default)


SITE_URL = (_cfg("SITE_URL", "http://localhost") or "http://localhost").rstrip("/")
SITE_PORT = (_cfg("SITE_PORT", "8080") or "8080").strip()
APP_UPSTREAM = (_cfg("APP_UPSTREAM", "http://127.0.0.1:8081") or "http://127.0.0.1:8081").rstrip("/")

_URL_SAFE = "/?:=&%-._~"

IDP_LIST = [
    item.strip().lower()
    for item in _cfg("IDP_LIST", "entra").split(",")
    if item.strip()
]
if not IDP_LIST:
    IDP_LIST = ["entra"]

DEFAULT_IDP = (_cfg("DEFAULT_IDP") or "").strip().lower() or None
if DEFAULT_IDP and DEFAULT_IDP not in IDP_LIST:
    DEFAULT_IDP = None

_OAUTH2_PROXY_PORT_BASE = int(_cfg("OAUTH2_PROXY_PORT_BASE", "4180"))
_IDP_PORT_MAP: dict[str, int] = {
    idp: _OAUTH2_PROXY_PORT_BASE + i for i, idp in enumerate(IDP_LIST)
}

_IDP_DEFAULT_KIND: dict[str, str] = {
    "entra":    "microsoft",
    "google":   "google",
    "apple":    "apple",
    "facebook": "facebook",
    "github":   "github",
}

_KIND_AUTH_PROVIDER: dict[str, str] = {
    "microsoft":      "aad",
    "apple":          "apple",
    "google":         "google",
    "openid-connect": "oidc",
    "oidc":           "oidc",
    "facebook":       "facebook",
    "github":         "github",
}

_IDP_DEFAULT_DISPLAY_NAME: dict[str, str] = {
    "entra":          "Microsoft",
    "google":         "Google",
    "apple":          "Apple",
    "facebook":       "Facebook",
    "github":         "GitHub",
    "openid-connect": "OpenID Connect",
}

_KIND_USER_ID_CLAIM: dict[str, str] = {
    "microsoft":      "preferred_username",
    "apple":          "email",
    "google":         "email",
    "openid-connect": "sub",
    "oidc":           "sub",
    "facebook":       "id",
    "github":         "login",
}

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

# Simple Icons CDN slugs (https://simpleicons.org, CC0 licensed), keyed by KIND.
# Providers not listed here fall back to the generic icon.
_KIND_SIMPLE_ICONS: dict[str, str] = {
    "google":         "google",
    "apple":          "apple",
    "github":         "github",
    "facebook":       "facebook",
    "oidc":           "openid",
    "openid-connect": "openid",
}
_SIMPLEICONS_SLUG_RE = re.compile(r'^[a-z0-9-]+$')

# IDP_SELECT_ICONS controls icon style on /.auth/login/select.
#   simple   — Simple Icons CDN logo (default)
#   generic  — generic ID card icon (fully offline)
#   text     — no icon, text labels only
IDP_SELECT_ICONS = (_cfg("IDP_SELECT_ICONS", "simple") or "simple").strip().lower()
if IDP_SELECT_ICONS not in ("simple", "generic", "text"):
    IDP_SELECT_ICONS = "simple"

_IDP_ICON_GENERIC = (
    '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#6366f1"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="2" y="5" width="20" height="14" rx="2"/>'
    '<circle cx="8" cy="12" r="2.5"/>'
    '<path d="M13 10h4M13 14h4"/>'
    '</svg>'
)


def _idp_icon_html(idp: str) -> str:
    if IDP_SELECT_ICONS == "text":
        return ""
    if IDP_SELECT_ICONS == "generic":
        return _IDP_ICON_GENERIC
    icon_cfg = (_cfg(f"{_idp_cfg_prefix(idp)}_ICON") or "").strip()
    if icon_cfg:
        if icon_cfg.startswith(("http://", "https://")):
            safe_url = html.escape(icon_cfg, quote=True)
            return f'<img class="icon" src="{safe_url}" width="22" height="22" alt="">'
        if _SIMPLEICONS_SLUG_RE.match(icon_cfg):
            return f'<img class="icon" src="https://cdn.simpleicons.org/{icon_cfg}" width="22" height="22" alt="">'
        return _IDP_ICON_GENERIC
    kind = _idp_kind(idp) or _IDP_DEFAULT_KIND.get(idp, "")
    slug = _KIND_SIMPLE_ICONS.get(kind)
    if not slug:
        return _IDP_ICON_GENERIC
    return f'<img class="icon" src="https://cdn.simpleicons.org/{slug}" width="22" height="22" alt="">'

_AUTH_HEADERS_TO_STRIP = frozenset({
    "x-forwarded-user", "x-forwarded-email",
    "x-ms-client-principal", "x-ms-client-principal-id",
    "x-ms-client-principal-idp", "x-ms-client-principal-name",
    "x-ms-token-aad-access-token", "x-ms-token-aad-id-token",
    "x-auth-request-user", "x-auth-request-email",
    "x-auth-request-access-token", "x-auth-request-id-token",
    "x-easyauth-user-id-claim",
})


def _parse_bool_cfg(name: str, default: str = "false") -> bool:
    return _cfg(name, default).lower() in ("1", "true", "yes", "on")


DEBUG_HEADERS_ENDPOINT_ENABLED = _parse_bool_cfg("DEBUG_HEADERS_ENDPOINT_ENABLED")
TLS_CERT_FILE = (_cfg("TLS_CERT_FILE") or "").strip()
TLS_KEY_FILE  = (_cfg("TLS_KEY_FILE")  or "").strip()
_TLS_ENABLED  = bool(TLS_CERT_FILE and TLS_KEY_FILE)
# Fallback protocol when the request carries no X-Forwarded-Proto — also
# honour an https SITE_URL, which covers TLS-terminating front ends (tunnel
# domains, reverse proxies) that may not send the header.
_DEFAULT_PROTO = "https" if (_TLS_ENABLED or SITE_URL.startswith("https://")) else "http"
COOKIE_SECURE = _parse_bool_cfg("OAUTH2_PROXY_COOKIE_SECURE") or _TLS_ENABLED


def _parse_skip_routes(raw: str) -> "list[tuple[str, re.Pattern]]":
    routes = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            method, pattern = entry.split("=", 1)
            routes.append((method.upper(), re.compile(pattern)))
        else:
            routes.append(("*", re.compile(entry)))
    return routes


SKIP_AUTH_ROUTES = _parse_skip_routes(_cfg("SKIP_AUTH_ROUTES") or "")


def _idp_cfg_prefix(idp: str) -> str:
    return f"IDP_{idp.upper().replace('-', '_')}"


IDP_DISPLAY_NAMES = {
    idp: (_cfg(f"IDP_{idp.upper().replace('-', '_')}_DISPLAY_NAME") or _IDP_DEFAULT_DISPLAY_NAME.get(idp, idp))
    for idp in IDP_LIST
}

# ---------------------------------------------------------------------------
# IDP metadata helpers
# ---------------------------------------------------------------------------


def _idp_auth_provider(idp: str) -> str:
    up = idp.upper().replace("-", "_")
    kind = (_cfg(f"IDP_{up}_KIND") or "").lower() or _IDP_DEFAULT_KIND.get(idp, "openid-connect")
    default = _KIND_AUTH_PROVIDER.get(kind, idp)
    return (_cfg(f"IDP_{up}_AUTH_PROVIDER") or default)


def _idp_user_id_claim(idp: str) -> str:
    up = idp.upper().replace("-", "_")
    kind = (_cfg(f"IDP_{up}_KIND") or "").lower() or _IDP_DEFAULT_KIND.get(idp, "openid-connect")
    default = _KIND_USER_ID_CLAIM.get(kind, "sub")
    return (_cfg(f"IDP_{up}_AUTH_USER_ID_CLAIM") or default)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _idp_kind(idp: str) -> str:
    return (_cfg(f"{_idp_cfg_prefix(idp)}_KIND") or "").strip().lower()


def _idp_oidc_issuer(idp: str) -> str:
    return (_cfg(f"{_idp_cfg_prefix(idp)}_OIDC_ISSUER_URL") or "").strip()


def _idp_logout_endpoint(idp: str) -> str:
    explicit = (_cfg(f"{_idp_cfg_prefix(idp)}_LOGOUT_ENDPOINT") or "").strip()
    if explicit:
        return explicit
    kind = _idp_kind(idp)
    issuer = _idp_oidc_issuer(idp).rstrip("/")
    if kind == "microsoft" and issuer.lower().endswith("/v2.0"):
        base_issuer = issuer[: -len("/v2.0")]
        return f"{base_issuer}/oauth2/v2.0/logout"
    return ""


def _build_provider_logout_url(idp: str, post_logout_redirect_uri: str,
                               proto: str = "", host: str = "") -> str:
    endpoint = _idp_logout_endpoint(idp)
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    absolute_post_logout_redirect_uri = post_logout_redirect_uri
    if not urlparse(absolute_post_logout_redirect_uri).scheme:
        if host:
            # Follow the origin the browser actually used (Host header),
            # like the OAuth callback URL — SITE_URL/SITE_PORT are only a
            # fallback for requests without a Host header.
            base_site_url = f"{proto or _DEFAULT_PROTO}://{host}"
        else:
            parsed_site = urlparse(SITE_URL)
            default_port = "443" if parsed_site.scheme == "https" else "80"
            base_site_url = SITE_URL
            # An https SITE_URL without local TLS files means a TLS-terminating
            # front end serves the public origin — SITE_PORT is only the local
            # listen port then, not part of the public URL.
            behind_tls_front = parsed_site.scheme == "https" and not _TLS_ENABLED
            if SITE_PORT != default_port and not behind_tls_front:
                base_site_url = f"{SITE_URL}:{SITE_PORT}"
        absolute_post_logout_redirect_uri = urljoin(
            f"{base_site_url}/", post_logout_redirect_uri.lstrip("/")
        )
    query_items.setdefault("post_logout_redirect_uri", absolute_post_logout_redirect_uri)
    rebuilt = parsed._replace(query=urlencode(query_items, doseq=True))
    return urlunparse(rebuilt)


def _provider_logout_bridge_url(idp: str, post_logout_redirect_uri: str) -> str:
    return f"/.auth/provider_logout/{idp}?{urlencode({'post_logout_redirect_uri': post_logout_redirect_uri})}"


def _safe_redirect(url: str) -> str:
    if url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


def _decode_principal(header_value: str):
    if not header_value:
        return None
    try:
        decoded = base64.b64decode(header_value).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None


def _decode_jwt_claims(token: str) -> "dict[str, object]":
    if not token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _compute_client_principal(user: str, email: str, auth_provider: str, user_id_claim: str, id_token: str = "") -> str:
    name = email or user
    if not user and not email and not name:
        return ""
    jwt_claims = _decode_jwt_claims(id_token)
    if jwt_claims:
        claims = [{"typ": k, "val": str(v) if not isinstance(v, list) else ",".join(str(i) for i in v)}
                  for k, v in jwt_claims.items()]
    else:
        claims = []
        if user:
            claims.append({"typ": user_id_claim, "val": user})
        if email:
            claims.append({"typ": "emails", "val": email})
        if name:
            claims.append({"typ": "name", "val": name})
    principal = {
        "auth_typ": auth_provider,
        "name_typ": user_id_claim,
        "role_typ": "roles",
        "claims": claims,
    }
    return base64.b64encode(
        json.dumps(principal, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _check_auth(idp: str, cookie: str, real_ip: str, proto: str, host: str, uri: str) -> "dict[str, str] | None":
    """
    Call the IDP's oauth2-proxy /oauth2/auth endpoint.
    Returns enriched auth headers on success (2xx), None on 401/403/error.
    """
    port = _IDP_PORT_MAP.get(idp)
    if port is None:
        return None
    auth_url = f"http://127.0.0.1:{port}/oauth2/auth"
    auth_provider = _idp_auth_provider(idp)
    user_id_claim = _idp_user_id_claim(idp)

    req = UrlRequest(auth_url)
    if cookie:
        req.add_header("Cookie", cookie)
    if real_ip:
        req.add_header("X-Real-IP", real_ip)
    if proto:
        req.add_header("X-Forwarded-Proto", proto)
    if host:
        req.add_header("X-Forwarded-Host", host)
    if uri:
        req.add_header("X-Forwarded-Uri", uri)

    try:
        with urlopen(req, timeout=10) as resp:
            auth_user    = resp.getheader("X-Auth-Request-User", "") or ""
            auth_email   = resp.getheader("X-Auth-Request-Email", "") or ""
            access_token = resp.getheader("X-Auth-Request-Access-Token", "") or ""
            id_token     = resp.getheader("X-Auth-Request-Id-Token", "") or ""
            # --set-authorization-header sets Authorization: Bearer <id_token> for OIDC
            if not id_token:
                auth_header = resp.getheader("Authorization", "") or ""
                if auth_header.lower().startswith("bearer "):
                    id_token = auth_header[7:].strip()

            principal_b64 = _compute_client_principal(auth_user, auth_email, auth_provider, user_id_claim, id_token)
            principal_id  = auth_user or auth_email

            result: dict[str, str] = {}
            if auth_user:     result["X-Forwarded-User"]            = auth_user
            if auth_email:    result["X-Forwarded-Email"]           = auth_email
            if auth_email:    result["X-MS-CLIENT-PRINCIPAL-NAME"]  = auth_email
            if access_token:  result["X-MS-TOKEN-AAD-ACCESS-TOKEN"] = access_token
            if id_token:      result["X-MS-TOKEN-AAD-ID-TOKEN"]     = id_token
            if principal_b64: result["X-MS-CLIENT-PRINCIPAL"]       = principal_b64
            if principal_id:  result["X-MS-CLIENT-PRINCIPAL-ID"]    = principal_id
            return result
    except HTTPError:
        return None
    except (URLError, OSError):
        return None


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "EasyAuthNative/1.0"
    # HTTP/1.1 persistent connections. Front ends that pool backend
    # connections (e.g. the dev tunnels edge) race against HTTP/1.0's
    # close-after-response and lose requests (intermittent hangs / 504s).
    # Requires every response to carry Content-Length — all response
    # helpers and _proxy_to do.
    protocol_version = "HTTP/1.1"
    # Drop keep-alive connections after 2 minutes of inactivity so idle
    # front-end connections don't pin handler threads forever.
    timeout = 120

    def log_message(self, *_) -> None:
        return

    # --- Routing ---

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path

        if path == "/healthz":
            self._send_text("ok")
        elif path == "/.auth/me":
            self._handle_auth_me()
        elif path == "/.auth/login":
            self._handle_auth_login_index()
        elif path == "/.auth/login/select":
            self._handle_auth_login_select()
        elif path == "/.auth/login/aad":
            self._handle_auth_login_aad()
        elif path.startswith("/.auth/login/"):
            self._handle_auth_login_idp(path[len("/.auth/login/"):])
        elif path == "/.auth/logout":
            self._handle_auth_logout()
        elif path.startswith("/.auth/provider_logout/"):
            self._handle_auth_provider_logout(path[len("/.auth/provider_logout/"):])
        elif path == "/.auth/refresh":
            self._handle_auth_refresh()
        elif path.startswith("/.auth/"):
            self._send_empty(404)
        elif path == "/.debug/headers":
            self._handle_debug_headers()
        elif path.startswith("/oauth2/"):
            self._handle_oauth2_proxy(path)
        else:
            self._handle_protected()

    def do_GET(self)     -> None: self._dispatch()
    def do_POST(self)    -> None: self._dispatch()
    def do_PUT(self)     -> None: self._dispatch()
    def do_DELETE(self)  -> None: self._dispatch()
    def do_PATCH(self)   -> None: self._dispatch()
    def do_HEAD(self)    -> None: self._dispatch()
    def do_OPTIONS(self) -> None: self._dispatch()

    # --- Request accessors ---

    def _header(self, name: str) -> str:
        return self.headers.get(name, "") or ""

    def _query_param(self, name: str, default: str = "") -> str:
        params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        return params.get(name, [default])[0]

    def _cookies(self) -> dict[str, str]:
        sc = SimpleCookie()
        sc.load(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in sc.items()}

    def _client_ip(self) -> str:
        fwd = self._header("X-Forwarded-For").split(",")[0].strip()
        return fwd or self.client_address[0]

    # --- Response helpers ---

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, markup: str, status: int = 200) -> None:
        body = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int, extra_headers: "dict[str, str] | None" = None) -> None:
        self.send_response(status)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect(self, url: str, status: int = 302,
                   cookies: "list[str] | None" = None) -> None:
        self.send_response(status)
        self.send_header("Location", url)
        for cookie in (cookies or []):
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- Cookie builders ---

    def _make_cookie(self, name: str, value: str, max_age: "int | None" = None) -> str:
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def _erase_cookie(self, name: str) -> str:
        return f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"

    # --- IDP selection ---

    def _current_idp(self) -> str:
        """Mirror the nginx map chain: session cookie → easyauth_idp cookie → DEFAULT_IDP → first IDP (if only one)."""
        cookies = self._cookies()
        session_idp = ""
        for idp in IDP_LIST:
            base = f"_oauth2_proxy_{idp}"
            for name in cookies:
                if name == base or (name.startswith(f"{base}_") and name[len(base) + 1:].isdigit()):
                    session_idp = idp
                    break
            if session_idp:
                break
        idp_input = session_idp or cookies.get("easyauth_idp", "").strip().lower()
        if idp_input in IDP_LIST:
            return idp_input
        if DEFAULT_IDP:
            return DEFAULT_IDP
        if len(IDP_LIST) == 1:
            return IDP_LIST[0]
        return ""

    # --- Proxy helper ---

    def _proxy_to(self, target_base: str, path_override: "str | None" = None,
                  extra_headers: "dict[str, str] | None" = None,
                  strip_headers: "frozenset[str] | None" = None) -> None:
        parsed = urlsplit(target_base)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target_path = (parsed.path or "").rstrip("/")

        req_path = path_override if path_override is not None else self.path
        if target_path:
            req_path = target_path + req_path

        strip = (strip_headers or frozenset()) | _HOP_BY_HOP
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if name.lower() in strip:
                continue
            headers[name] = value
        if extra_headers:
            headers.update(extra_headers)

        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length) if content_length > 0 else None

        conn: "http.client.HTTPConnection | None" = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request(self.command, req_path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()

            self.send_response(resp.status)
            skip = _HOP_BY_HOP | frozenset({"content-length"})
            for hname, hvalue in resp.getheaders():
                if hname.lower() in skip:
                    continue
                self.send_header(hname, hvalue)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception:
            self._send_empty(502)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # --- Route handlers ---

    def _handle_auth_me(self) -> None:
        idp = self._current_idp()
        if not idp:
            self._send_json([])
            return
        auth_result = _check_auth(
            idp,
            cookie=self._header("Cookie"),
            real_ip=self._client_ip(),
            proto=self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
            host=self._header("Host"),
            uri=self.path,
        )
        if not auth_result:
            self._send_json([])
            return
        user         = auth_result.get("X-Forwarded-User", "")
        email        = auth_result.get("X-Forwarded-Email", "")
        access_token = auth_result.get("X-MS-TOKEN-AAD-ACCESS-TOKEN", "")
        id_token     = auth_result.get("X-MS-TOKEN-AAD-ID-TOKEN", "")
        user_id_claim = _idp_user_id_claim(idp)
        auth_provider = _idp_auth_provider(idp)
        principal_name = email or user
        if not principal_name:
            self._send_json([])
            return

        jwt_claims = _decode_jwt_claims(id_token)
        if jwt_claims:
            user_claims = [
                {"typ": k, "val": str(v) if not isinstance(v, list) else ",".join(str(i) for i in v)}
                for k, v in jwt_claims.items()
            ]
        else:
            user_claims = []
            if user:           user_claims.append({"typ": user_id_claim, "val": user})
            if email:          user_claims.append({"typ": "emails", "val": email})
            if principal_name: user_claims.append({"typ": "name", "val": principal_name})

        exp = jwt_claims.get("exp") if jwt_claims else None
        if exp is not None:
            try:
                expires_on = datetime.datetime.fromtimestamp(int(exp), tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                expires_on = ""
        else:
            expires_on = ""

        self._send_json([{
            "provider_name":  auth_provider,
            "user_id":        user or email,
            "user_claims":    user_claims,
            "access_token":   access_token,
            "id_token":       id_token,
            "expires_on":     expires_on,
            "refresh_token":  None,
        }])

    def _handle_auth_login_index(self) -> None:
        rd = self._query_param("post_login_redirect_uri", "/")
        target = self._current_idp()
        if not target:
            self._redirect(f"/.auth/login/select?post_login_redirect_uri={quote(rd, safe=_URL_SAFE)}")
            return
        self._redirect(f"/.auth/login/{target}?post_login_redirect_uri={quote(rd, safe=_URL_SAFE)}")

    def _handle_auth_login_select(self) -> None:
        rd = self._query_param("post_login_redirect_uri", "/")
        buttons = "".join(
            f'<a class="btn" href="/.auth/login/{idp}?post_login_redirect_uri={quote(rd, safe=_URL_SAFE)}">'
            f'{_idp_icon_html(idp)}'
            f'<span>{IDP_DISPLAY_NAMES.get(idp, idp)}</span>'
            f'</a>'
            for idp in IDP_LIST
        )
        self._send_html(f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>Sign in</title>
    <style>
      *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
      body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}}
      .card{{background:#fff;border-radius:16px;box-shadow:0 2px 8px rgba(0,0,0,.08),0 8px 32px rgba(0,0,0,.06);padding:2.5rem 2rem;width:100%;max-width:360px;text-align:center}}
      .lock{{color:#6366f1;margin:0 auto 1.25rem;display:block}}
      h1{{font-size:1.375rem;font-weight:700;color:#111827;margin-bottom:.375rem;letter-spacing:-.01em}}
      .sub{{font-size:.875rem;color:#6b7280;margin-bottom:1.75rem}}
      .providers{{display:flex;flex-direction:column;gap:.625rem}}
      .btn{{display:flex;align-items:center;gap:.75rem;padding:.75rem 1rem;border:1.5px solid #e5e7eb;border-radius:10px;background:#fff;color:#111827;font-size:.9375rem;font-weight:500;text-decoration:none;transition:background .12s,border-color .12s,box-shadow .12s,transform .1s;text-align:left}}
      .btn:hover{{background:#f9fafb;border-color:#d1d5db;box-shadow:0 2px 8px rgba(0,0,0,.08);transform:translateY(-1px)}}
      .btn:active{{transform:translateY(0)}}
      .icon{{flex-shrink:0;width:22px;height:22px}}
      .btn span{{flex:1}}
      .text-mode .btn{{justify-content:center}}
    </style>
  </head>
  <body>
    <div class="card">
      <svg class="lock" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="11" rx="2"/>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
      <h1>Sign in</h1>
      <p class="sub">Choose a provider to continue</p>
      <div class="{'providers text-mode' if IDP_SELECT_ICONS == 'text' else 'providers'}">{buttons}</div>
    </div>
  </body>
</html>""")

    def _handle_auth_login_idp(self, idp: str) -> None:
        normalized = idp.strip().lower()
        if normalized not in IDP_LIST:
            self._send_json({"error": "unknown idp", "idp": normalized}, status=404)
            return
        rd = self._query_param("post_login_redirect_uri", "/")
        self._redirect(
            f"/oauth2/{normalized}/start?rd={quote(rd, safe=_URL_SAFE)}",
            cookies=[self._make_cookie("easyauth_idp", normalized, max_age=60 * 60 * 24 * 30)],
        )

    def _handle_auth_login_aad(self) -> None:
        rd = self._query_param("post_login_redirect_uri", "/")
        target = "entra" if "entra" in IDP_LIST else self._current_idp()
        if not target:
            self._redirect(f"/.auth/login/select?post_login_redirect_uri={quote(rd, safe=_URL_SAFE)}")
            return
        self._redirect(f"/.auth/login/{target}?post_login_redirect_uri={quote(rd, safe=_URL_SAFE)}")

    def _handle_auth_logout(self) -> None:
        rd = _safe_redirect(self._query_param("post_logout_redirect_uri", "/"))
        selected = self._current_idp()
        if not selected:
            self._redirect(rd, cookies=[self._erase_cookie("easyauth_idp")])
            return
        logout_target = _provider_logout_bridge_url(selected, rd)
        sign_out_url = f"/oauth2/{selected}/sign_out?{urlencode({'rd': logout_target})}"
        self._redirect(sign_out_url, cookies=[self._erase_cookie("easyauth_idp")])

    def _handle_auth_provider_logout(self, idp: str) -> None:
        normalized = idp.strip().lower()
        rd = _safe_redirect(self._query_param("post_logout_redirect_uri", "/"))
        provider_logout_url = _build_provider_logout_url(
            normalized, rd,
            proto=self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
            host=self._header("Host"),
        )
        if provider_logout_url:
            self._redirect(provider_logout_url)
            return
        self._redirect(rd)

    def _handle_auth_refresh(self) -> None:
        idp = self._current_idp()
        if not idp:
            self._send_empty(401)
            return
        auth_result = _check_auth(
            idp,
            cookie=self._header("Cookie"),
            real_ip=self._client_ip(),
            proto=self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
            host=self._header("Host"),
            uri=self.path,
        )
        if not auth_result:
            self._send_empty(401)
            return
        self._send_empty(200)

    def _handle_debug_headers(self) -> None:
        if not DEBUG_HEADERS_ENDPOINT_ENABLED:
            self._send_json({"error": "not found"}, status=404)
            return
        idp = self._current_idp()
        auth_result: dict[str, str] = {}
        if idp:
            auth_result = _check_auth(
                idp,
                cookie=self._header("Cookie"),
                real_ip=self._client_ip(),
                proto=self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
                host=self._header("Host"),
                uri=self.path,
            ) or {}
        principal_raw = auth_result.get("X-MS-CLIENT-PRINCIPAL", "")
        self._send_json({
            "enabled":                         True,
            "x-ms-client-principal-id":        auth_result.get("X-MS-CLIENT-PRINCIPAL-ID", ""),
            "x-ms-client-principal-idp":       _idp_auth_provider(idp) if idp else "",
            "x-ms-client-principal-name":      auth_result.get("X-MS-CLIENT-PRINCIPAL-NAME", ""),
            "x-ms-client-principal":           principal_raw,
            "x-ms-client-principal-decoded":   _decode_principal(principal_raw),
            "default-idp":                     DEFAULT_IDP or "",
            "selected-idp-cookie":             self._cookies().get("easyauth_idp", ""),
            "effective-idp":                   idp,
            "x-easyauth-user-id-claim":        _idp_user_id_claim(idp) if idp else "",
            "x-ms-token-aad-access-token-present": bool(auth_result.get("X-MS-TOKEN-AAD-ACCESS-TOKEN", "")),
            "x-ms-token-aad-id-token-present":     bool(auth_result.get("X-MS-TOKEN-AAD-ID-TOKEN", "")),
            "x-forwarded-user":                auth_result.get("X-Forwarded-User", ""),
            "x-forwarded-email":               auth_result.get("X-Forwarded-Email", ""),
        })

    def _handle_oauth2_proxy(self, path: str) -> None:
        if path == "/oauth2/auth":
            self._send_empty(404)
            return
        if path == "/oauth2/callback":
            idp = self._current_idp()
            port = _IDP_PORT_MAP.get(idp)
            if not port:
                self._send_empty(502)
                return
            self._proxy_to(f"http://127.0.0.1:{port}")
            return
        for idp in IDP_LIST:
            prefix = f"/oauth2/{idp}/"
            if path.startswith(prefix) or path == f"/oauth2/{idp}":
                port = _IDP_PORT_MAP[idp]
                sub = path[len(f"/oauth2/{idp}"):]
                if not sub:
                    sub = "/"
                qs = urlsplit(self.path).query
                new_path = f"/oauth2{sub}" + (f"?{qs}" if qs else "")
                self._proxy_to(f"http://127.0.0.1:{port}", path_override=new_path)
                return
        self._send_empty(404)

    def _handle_protected(self) -> None:
        path = urlsplit(self.path).path
        for method, pattern in SKIP_AUTH_ROUTES:
            if (method == "*" or method == self.command) and pattern.search(path):
                self._proxy_to(APP_UPSTREAM, strip_headers=_AUTH_HEADERS_TO_STRIP)
                return

        idp = self._current_idp()
        if not idp:
            self._redirect(f"/.auth/login?post_login_redirect_uri={quote(self.path, safe=_URL_SAFE)}")
            return
        auth_result = _check_auth(
            idp,
            cookie=self._header("Cookie"),
            real_ip=self._client_ip(),
            proto=self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
            host=self._header("Host"),
            uri=self.path,
        )
        if auth_result is None:
            self._redirect(f"/.auth/login?post_login_redirect_uri={quote(self.path, safe=_URL_SAFE)}")
            return
        extra: dict[str, str] = {
            "X-Real-IP":               self._client_ip(),
            "X-Forwarded-Proto":       self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
            "X-Forwarded-Host":        self._header("Host"),
            "X-MS-CLIENT-PRINCIPAL-IDP":  _idp_auth_provider(idp),
            "X-Easyauth-User-Id-Claim":   _idp_user_id_claim(idp),
        }
        extra.update(auth_result)
        self._proxy_to(APP_UPSTREAM, extra_headers=extra, strip_headers=_AUTH_HEADERS_TO_STRIP)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    port = int(SITE_PORT)
    if sys.platform == "win32":
        # On Windows, SO_REUSEADDR lets a second process bind an in-use port,
        # silently splitting requests between two gateway instances (e.g. a
        # stale emulator left over from a crashed session) — fail fast instead.
        ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        print(
            f"[app] ERROR: cannot listen on port {port}: {exc} — "
            f"is another EasyAuth Emulator (or a stale instance) still running?",
            file=sys.stderr,
        )
        sys.exit(1)
    if _TLS_ENABLED:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
        except Exception as exc:
            print(f"[app] ERROR: Failed to load TLS certificate: {exc}", file=sys.stderr)
            sys.exit(1)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"[app] Listening on https://0.0.0.0:{port}")
    else:
        print(f"[app] Listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

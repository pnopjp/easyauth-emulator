#!/usr/bin/env python3
import base64
import datetime
import html
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import tomllib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse, urlsplit
from urllib.request import Request as UrlRequest, urlopen

import h2.config
import h2.connection
import h2.events
import h2.exceptions

# Force UTF-8 output regardless of the system locale (e.g. cp932 on Japanese
# Windows) — same rationale as start.py's identical reconfigure call. This
# process also runs standalone (not just as start.py's subprocess), so it
# needs its own fix.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
# HTTP/2 carves out an explicit exception for TE (RFC 7540 8.1.2.2): it MAY be
# present, and MUST be "trailers" when it is — this is exactly how gRPC
# clients signal they can receive trailers (grpc-status/grpc-message). Strip
# it like any other HTTP/1.1 hop-by-hop header and grpc-only/all upstream
# relaying silently breaks (the upstream never sends a trailers frame back).
_HOP_BY_HOP_HTTP2 = _HOP_BY_HOP - frozenset({"te"})

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

# Client-facing HTTP/2 is additive to HTTP/1.1 on SITE_PORT (both are accepted,
# matching App Service's "HTTP version: 2.0" — not exclusive). HTTP20_PROXY_MODE
# controls how much of that is preserved end to end to APP_UPSTREAM, matching
# App Service's http20ProxyFlag: "disabled" transparently downgrades to
# HTTP/1.1 (fine for ordinary request/response traffic; breaks gRPC, which
# cannot be represented over HTTP/1.1), "all" preserves HTTP/2 for every
# request, "grpc-only" preserves it only for requests whose Content-Type
# starts with application/grpc. HTTP20_PROXY_MODE only has any effect while
# HTTP20_ENABLED is on — with it off, SITE_PORT never receives an HTTP/2
# request to preserve in the first place, so every request is relayed as
# HTTP/1.1 regardless of HTTP20_PROXY_MODE (APPSERVICE_HTTP20_ONLY_PORT below
# is the one exception: that dedicated port is HTTP/2 end to end regardless
# of HTTP20_ENABLED).
HTTP20_ENABLED = _parse_bool_cfg("HTTP20_ENABLED")
HTTP20_PROXY_MODE = (_cfg("HTTP20_PROXY_MODE", "disabled") or "disabled").strip().lower()
if HTTP20_PROXY_MODE not in ("disabled", "all", "grpc-only"):
    HTTP20_PROXY_MODE = "disabled"

# Dedicated HTTP/2-only port mirroring Azure App Service's HTTP20_ONLY_PORT
# app setting (the separate port App Service requires for gRPC, distinct
# from SITE_PORT — unlike Azure Container Apps, which multiplexes gRPC on a
# single ingress port and has no equivalent of this). Prefixed with
# APPSERVICE_ (rather than reusing Azure's literal name verbatim) since,
# unlike HTTP20_ENABLED/HTTP20_PROXY_MODE, this whole port topology has no
# Container Apps counterpart at all — the prefix signals at a glance that
# Container Apps users can leave it unset.
_APPSERVICE_HTTP20_ONLY_PORT_RAW = (_cfg("APPSERVICE_HTTP20_ONLY_PORT") or "").strip()
try:
    APPSERVICE_HTTP20_ONLY_PORT = int(_APPSERVICE_HTTP20_ONLY_PORT_RAW) if _APPSERVICE_HTTP20_ONLY_PORT_RAW else None
except ValueError:
    APPSERVICE_HTTP20_ONLY_PORT = None


def _build_upstream_ssl_context(alpn_protocols: "list[str] | None" = None) -> ssl.SSLContext:
    """TLS context used when APP_UPSTREAM is https:// (both the HTTP/1.1 and
    HTTP/2 relay paths in _proxy_to). Reuses SSL_CA_BUNDLE — previously only
    documented for start.py's own outbound GitHub calls — since it's the same
    "outbound HTTPS this emulator makes" trust concern. Without an explicit
    bundle, use truststore's own SSLContext class directly (NOT
    truststore.inject_into_ssl(), which monkey-patches ssl.SSLContext
    globally — this process also builds a plain server-side ssl.SSLContext
    for its own TLS listener in main(), and truststore's wrap_socket()
    unconditionally runs client-side peer verification even when
    server_side=True, breaking that listener) so locally-issued dev certs
    (e.g. mkcert, once installed into the OS store) verify without extra
    configuration."""
    ca_bundle = (_cfg("SSL_CA_BUNDLE") or "").strip()
    if ca_bundle:
        ctx = ssl.create_default_context(cafile=ca_bundle)
    else:
        try:
            import truststore
            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            ctx = ssl.create_default_context()
    if alpn_protocols:
        ctx.set_alpn_protocols(alpn_protocols)
    return ctx


# Two separate contexts, not one shared+mutated one: concurrent requests can
# hit the HTTP/1.1 and HTTP/2 relay paths at the same time, and SSLContext
# ALPN settings aren't meant to be flipped per-call on a shared instance.
_UPSTREAM_SSL_CONTEXT = _build_upstream_ssl_context()
_UPSTREAM_SSL_CONTEXT_H2 = _build_upstream_ssl_context(alpn_protocols=["h2"])


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


class _RoutingMixin:
    """Transport-agnostic request routing shared by the HTTP/1.1 (_Handler)
    and HTTP/2 (_Http2StreamHandler) implementations.

    A concrete subclass must set the `path` and `command` attributes for the
    current request, and implement the primitives below (_header,
    _all_headers, _peer_ip, _read_request_body, and the _send_*/_redirect/
    _send_raw_response writers). Every other method here — routing,
    .auth/* handlers, _proxy_to — is transport-independent and shared as-is.
    """

    path: str
    command: str

    # --- Primitives a concrete transport must implement ---

    def _header(self, name: str) -> str:
        raise NotImplementedError

    def _all_headers(self) -> "list[tuple[str, str]]":
        raise NotImplementedError

    def _peer_ip(self) -> str:
        raise NotImplementedError

    def _scheme(self) -> str:
        """'http' or 'https', authoritatively determined from how this
        specific request actually arrived (the connection's own TLS state,
        or the HTTP/2 :scheme pseudo-header) — used as the fallback when the
        client sent no X-Forwarded-Proto of its own."""
        raise NotImplementedError

    def _read_request_body(self) -> "bytes | None":
        raise NotImplementedError

    def _send_text(self, text: str, status: int = 200) -> None:
        raise NotImplementedError

    def _send_html(self, markup: str, status: int = 200) -> None:
        raise NotImplementedError

    def _send_json(self, data, status: int = 200) -> None:
        raise NotImplementedError

    def _send_empty(self, status: int, extra_headers: "dict[str, str] | None" = None) -> None:
        raise NotImplementedError

    def _redirect(self, url: str, status: int = 302,
                   cookies: "list[str] | None" = None) -> None:
        raise NotImplementedError

    def _send_raw_response(self, status: int, headers: "list[tuple[str, str]]", body: bytes) -> None:
        """Used only by _proxy_to to relay an upstream response's headers verbatim."""
        raise NotImplementedError

    def _stream_response(self, status: int, headers: "list[tuple[str, str]]",
                          chunks: "Iterable[bytes]") -> None:
        """Used only by _proxy_to's HTTP/1.1 upstream relay, to forward a
        response as its bytes arrive rather than buffering it all in memory
        first — the difference that makes SSE/streaming endpoints work
        instead of hanging until the upstream response completes."""
        raise NotImplementedError

    def _stream_response_with_trailers(self, status: int, headers: "list[tuple[str, str]]",
                                        chunks: "Iterable[bytes]", get_trailers) -> None:
        """Used only by _proxy_to's HTTP/2 upstream relay (HTTP20_PROXY_MODE
        "all"/"grpc-only"), to forward a response as it arrives. get_trailers
        is called only once chunks is fully exhausted and returns the
        trailers list (e.g. gRPC's grpc-status/grpc-message) collected along
        the way, or None. Trailers are meaningless over HTTP/1.1, and a
        genuine gRPC call can only ever arrive over a real HTTP/2 connection
        in the first place, so the default here just streams and ignores
        get_trailers — _Http2StreamHandler overrides this to send trailers as
        an actual trailing HEADERS frame."""
        self._stream_response(status, headers, chunks)

    def _send_response_with_trailers(self, status: int, headers: "list[tuple[str, str]]",
                                       body: bytes, trailers: "list[tuple[str, str]]") -> None:
        """Used only by _proxy_to when relaying an HTTP/2 upstream response (e.g.
        gRPC's grpc-status/grpc-message trailers) — trailers are meaningless
        over HTTP/1.1, and a genuine gRPC call can only ever arrive over a real
        HTTP/2 connection in the first place, so the default here (append them
        as ordinary headers) is an HTTP/1.1 fallback that in practice is never
        exercised for real gRPC traffic. _Http2StreamHandler overrides this to
        send trailers as an actual trailing HEADERS frame."""
        self._send_raw_response(status, list(headers) + list(trailers), body)

    # --- Request accessors built on the primitives above ---

    def _query_param(self, name: str, default: str = "") -> str:
        params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        return params.get(name, [default])[0]

    def _cookies(self) -> "dict[str, str]":
        sc = SimpleCookie()
        sc.load(self._header("Cookie"))
        return {k: v.value for k, v in sc.items()}

    def _client_ip(self) -> str:
        fwd = self._header("X-Forwarded-For").split(",")[0].strip()
        return fwd or self._peer_ip()

    def _effective_proto(self) -> str:
        """The scheme to report as X-Forwarded-Proto: an explicit header from
        a trusted front end in front of us (e.g. a tunnel edge) wins if
        present, otherwise our own authoritative per-request _scheme()."""
        return self._header("X-Forwarded-Proto") or self._scheme()

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

    def _is_grpc_request(self) -> bool:
        return self._header("Content-Type").lower().startswith("application/grpc")

    # Set to True on instances handling a connection accepted on
    # APPSERVICE_HTTP20_ONLY_PORT — see _wants_http2_upstream.
    _force_http2_upstream = False

    def _wants_http2_upstream(self) -> bool:
        """Whether this request should be relayed to APP_UPSTREAM over HTTP/2
        rather than downgraded to HTTP/1.1, per HTTP20_PROXY_MODE (mirrors
        App Service's http20ProxyFlag: disabled/all/grpc-only)."""
        if self._force_http2_upstream:
            # Arrived on APPSERVICE_HTTP20_ONLY_PORT — that dedicated port is
            # HTTP/2 end to end by definition, regardless of HTTP20_PROXY_MODE.
            return True
        if not HTTP20_ENABLED:
            # SITE_PORT never accepts HTTP/2 from a client in the first
            # place, so there is no inbound HTTP/2-ness for HTTP20_PROXY_MODE
            # to preserve — relaying to APP_UPSTREAM over HTTP/2 here would
            # upgrade a request that was never HTTP/2 to begin with.
            return False
        if HTTP20_PROXY_MODE == "all":
            return True
        if HTTP20_PROXY_MODE == "grpc-only":
            if APPSERVICE_HTTP20_ONLY_PORT:
                # gRPC is expected to arrive via the dedicated port instead,
                # so SITE_PORT no longer special-cases it by Content-Type.
                return False
            return self._is_grpc_request()
        return False

    def _proxy_to(self, target_base: str, path_override: "str | None" = None,
                  extra_headers: "dict[str, str] | None" = None,
                  strip_headers: "frozenset[str] | None" = None,
                  force_http1: bool = False) -> None:
        parsed = urlsplit(target_base)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target_path = (parsed.path or "").rstrip("/")

        req_path = path_override if path_override is not None else self.path
        if target_path:
            req_path = target_path + req_path

        # HTTP20_PROXY_MODE governs only the APP_UPSTREAM relay (mirrors Azure
        # App Service's http20ProxyFlag). oauth2-proxy is an internal
        # implementation detail of this emulator, not "the app" — it only
        # ever speaks plain HTTP/1.1, so callers proxying to it pass
        # force_http1=True to bypass HTTP20_PROXY_MODE entirely.
        relay_as_http2 = False if force_http1 else self._wants_http2_upstream()
        strip = (strip_headers or frozenset()) | (_HOP_BY_HOP_HTTP2 if relay_as_http2 else _HOP_BY_HOP)
        headers: dict[str, str] = {}
        for name, value in self._all_headers():
            if name.lower() in strip:
                continue
            headers[name] = value
        if extra_headers:
            headers.update(extra_headers)

        body = self._read_request_body()

        upstream_tls = parsed.scheme == "https"

        if relay_as_http2:
            relay = _http2_relay_request(host, port, self.command, req_path, headers, body, tls=upstream_tls)
            try:
                kind, status, resp_headers = next(relay)
                assert kind == "status"
            except Exception as exc:
                print(f"[app] upstream HTTP/2 relay to {host}:{port} (tls={upstream_tls}) failed: {exc!r}", file=sys.stderr)
                self._send_empty(502)
                return

            # relay is exhausted by _iter_relay_data below; get_trailers is
            # only meaningful (and only called) once that's happened, since
            # the "trailers" item is always the last thing the generator
            # yields — see _http2_relay_request's own docstring.
            trailers_box: "list[list[tuple[str, str]]]" = []

            def _iter_relay_data():
                for item in relay:
                    if item[0] == "data":
                        yield item[1]
                    elif item[0] == "trailers":
                        trailers_box.append(item[1])

            try:
                self._stream_response_with_trailers(
                    status, resp_headers, _iter_relay_data(),
                    lambda: (trailers_box[0] if trailers_box else None),
                )
            except Exception as exc:
                # Headers are already committed to the real client by this
                # point, so there's no clean error response left to send.
                print(f"[app] upstream HTTP/2 stream from {host}:{port} (tls={upstream_tls}) failed: {exc!r}", file=sys.stderr)
            return

        conn: "http.client.HTTPConnection | None" = None
        try:
            if upstream_tls:
                conn = http.client.HTTPSConnection(host, port, timeout=30, context=_UPSTREAM_SSL_CONTEXT)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request(self.command, req_path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            print(f"[app] upstream HTTP/1.1 relay to {host}:{port} (tls={upstream_tls}) failed: {exc!r}", file=sys.stderr)
            self._send_empty(502)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return

        # Streamed rather than buffered — resp.read(n) yields data as it
        # arrives (transparently de-chunking if the upstream used
        # Transfer-Encoding: chunked itself), which is what lets a
        # slow/unbounded response (SSE, etc.) reach the client incrementally
        # instead of hanging here until the upstream response completes.
        # Content-Length (if the upstream gave one) is passed through as-is —
        # only Transfer-Encoding is hop-by-hop and stripped, so an ordinary
        # complete response keeps its exact original framing; _stream_response
        # only switches to chunked encoding when there's no length to give.
        resp_headers = [(hname, hvalue) for hname, hvalue in resp.getheaders() if hname.lower() not in _HOP_BY_HOP]

        def _iter_upstream_chunks():
            while True:
                # read1, not read: plain read(n) blocks until n bytes have
                # arrived (or EOF) even if less is immediately available,
                # which for a slow-trickle response (SSE, etc.) would just
                # reintroduce buffering — read1 returns as soon as anything
                # is available, via at most one underlying raw read. The
                # 16384 cap keeps any single chunk under the default HTTP/2
                # flow-control window (65535 bytes) so it never needs
                # splitting when the client side of this response is HTTP/2.
                chunk = resp.read1(16384)
                if not chunk:
                    return
                yield chunk

        try:
            self._stream_response(resp.status, resp_headers, _iter_upstream_chunks())
        except Exception as exc:
            # Headers may already be on the wire by this point, so there's no
            # clean error response left to send — just log and give up.
            print(f"[app] upstream HTTP/1.1 stream from {host}:{port} (tls={upstream_tls}) failed: {exc!r}", file=sys.stderr)
        finally:
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
        # oauth2-proxy's --redirect-url is a bare path (see start.py), so it
        # builds the absolute redirect_uri it sends to the IdP from these two
        # headers on each request — without them it falls back to whatever
        # --cookie-secure implies (only ever true when the gateway itself
        # terminates TLS locally), silently producing a wrong-scheme (or, for
        # HTTP/2 clients with no Host header at all, wrong-host) redirect_uri.
        oauth2_headers = {
            "X-Forwarded-Proto": self._effective_proto(),
            "X-Forwarded-Host": self._header("Host"),
        }
        if path == "/oauth2/auth":
            self._send_empty(404)
            return
        if path == "/oauth2/callback":
            idp = self._current_idp()
            port = _IDP_PORT_MAP.get(idp)
            if not port:
                self._send_empty(502)
                return
            self._proxy_to(f"http://127.0.0.1:{port}", extra_headers=oauth2_headers, force_http1=True)
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
                self._proxy_to(f"http://127.0.0.1:{port}", path_override=new_path, extra_headers=oauth2_headers, force_http1=True)
                return
        self._send_empty(404)

    def _deny_unauthenticated(self) -> None:
        """Reject an unauthenticated request to a protected route. A gRPC
        client can't follow (or make any sense of) a browser-style redirect
        to /.auth/login — it would just hang until its own call deadline, as
        real gRPC clients speak nothing but HTTP/2 request/response. Real
        Azure App Service confirms this: its dedicated gRPC port returns a
        plain 401 (+ WWW-Authenticate: Bearer) rather than redirecting. The
        response still needs a gRPC-shaped Content-Type — without one, gRPC
        client libraries (e.g. grpcurl) reject it as a malformed header
        instead of mapping the bare 401 to an UNAUTHENTICATED status."""
        if self._is_grpc_request():
            self._send_empty(401, {
                "WWW-Authenticate": "Bearer",
                "Content-Type": "application/grpc",
            })
            return
        self._redirect(f"/.auth/login?post_login_redirect_uri={quote(self.path, safe=_URL_SAFE)}")

    def _is_websocket_upgrade_request(self) -> bool:
        return (
            "websocket" in self._header("Upgrade").lower()
            and "upgrade" in self._header("Connection").lower()
        )

    def _proxy_websocket(self, target_base: str, extra_headers: "dict[str, str] | None" = None) -> None:
        """Relay a WebSocket Upgrade handshake, then the raw bidirectional
        connection itself, until either side closes. RFC 6455's Upgrade
        mechanism is an HTTP/1.1 concept, so only _Handler implements this —
        WebSocket bootstrapping over HTTP/2 (RFC 8441, extended CONNECT) is a
        distinct, unimplemented feature; this default just rejects it
        cleanly. In practice this is never hit: h2's default SETTINGS
        (h2.config.H2Configuration, used unmodified by _Http2Connection)
        advertise ENABLE_CONNECT_PROTOCOL=0, and RFC 8441 requires a client
        to see that enabled before attempting it — so any compliant client
        just falls back to an ordinary HTTP/1.1 WebSocket connection instead,
        which _Handler already handles."""
        self._send_empty(501)

    def _handle_protected(self) -> None:
        path = urlsplit(self.path).path
        is_ws = self._is_websocket_upgrade_request()
        for method, pattern in SKIP_AUTH_ROUTES:
            if (method == "*" or method == self.command) and pattern.search(path):
                if is_ws:
                    self._proxy_websocket(APP_UPSTREAM)
                else:
                    self._proxy_to(APP_UPSTREAM, strip_headers=_AUTH_HEADERS_TO_STRIP)
                return

        idp = self._current_idp()
        if not idp:
            self._deny_unauthenticated()
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
            self._deny_unauthenticated()
            return
        extra: dict[str, str] = {
            "X-Real-IP":               self._client_ip(),
            "X-Forwarded-Proto":       self._header("X-Forwarded-Proto") or _DEFAULT_PROTO,
            "X-Forwarded-Host":        self._header("Host"),
            "X-MS-CLIENT-PRINCIPAL-IDP":  _idp_auth_provider(idp),
            "X-Easyauth-User-Id-Claim":   _idp_user_id_claim(idp),
        }
        extra.update(auth_result)
        if is_ws:
            self._proxy_websocket(APP_UPSTREAM, extra_headers=extra)
        else:
            self._proxy_to(APP_UPSTREAM, extra_headers=extra, strip_headers=_AUTH_HEADERS_TO_STRIP)


class _Handler(BaseHTTPRequestHandler, _RoutingMixin):
    """HTTP/1.1 transport: implements _RoutingMixin's primitives directly on
    top of BaseHTTPRequestHandler's own request/response I/O."""

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

    def do_GET(self)     -> None: self._dispatch()
    def do_POST(self)    -> None: self._dispatch()
    def do_PUT(self)     -> None: self._dispatch()
    def do_DELETE(self)  -> None: self._dispatch()
    def do_PATCH(self)   -> None: self._dispatch()
    def do_HEAD(self)    -> None: self._dispatch()
    def do_OPTIONS(self) -> None: self._dispatch()

    # --- _RoutingMixin primitives ---

    def _header(self, name: str) -> str:
        return self.headers.get(name, "") or ""

    def _all_headers(self) -> "list[tuple[str, str]]":
        return list(self.headers.items())

    def _peer_ip(self) -> str:
        return self.client_address[0]

    def _scheme(self) -> str:
        return "https" if isinstance(self.connection, ssl.SSLSocket) else "http"

    def _read_request_body(self) -> "bytes | None":
        if "chunked" in (self.headers.get("Transfer-Encoding", "") or "").lower():
            return self._read_chunked_body() or None
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(content_length) if content_length > 0 else None

    def _read_chunked_body(self) -> bytes:
        """Decode a Transfer-Encoding: chunked request body. _proxy_to relays
        the result as an ordinary sized body — Transfer-Encoding is hop-by-hop
        (_HOP_BY_HOP) and already stripped before forwarding, and http.client
        computes a fresh Content-Length from the decoded bytes on its own."""
        chunks = []
        while True:
            size_line = self.rfile.readline().strip()
            if not size_line:
                break
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

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

    def _send_raw_response(self, status: int, headers: "list[tuple[str, str]]", body: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _stream_response(self, status: int, headers: "list[tuple[str, str]]", chunks) -> None:
        # If the upstream gave a Content-Length, keep it — the response has a
        # known length, so it's streamed with its original, exact framing
        # (matching this emulator's long-standing behavior for ordinary
        # responses). Only when there's no length upfront (SSE, etc.) does
        # this switch to Transfer-Encoding: chunked, a standard HTTP/1.1
        # mechanism every real client already handles transparently.
        has_content_length = any(name.lower() == "content-length" for name, _ in headers)
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if not has_content_length:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            if has_content_length:
                for chunk in chunks:
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                return
            for chunk in chunks:
                if not chunk:
                    continue
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (ConnectionError, OSError):
            pass

    def _proxy_websocket(self, target_base: str, extra_headers: "dict[str, str] | None" = None) -> None:
        parsed = urlsplit(target_base)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target_path = (parsed.path or "").rstrip("/")
        req_path = target_path + self.path if target_path else self.path

        # Unlike _proxy_to, Connection/Upgrade are NOT stripped as hop-by-hop
        # here — they're exactly what's needed for the upstream to also
        # perform the Upgrade. Only the injected Easy Auth headers (a fresh
        # set is about to be added back in via extra_headers) are stripped.
        headers: dict[str, str] = {}
        for name, value in self._all_headers():
            if name.lower() in _AUTH_HEADERS_TO_STRIP:
                continue
            headers[name] = value
        if extra_headers:
            headers.update(extra_headers)

        try:
            upstream_sock = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            print(f"[app] upstream WebSocket connect to {host}:{port} failed: {exc!r}", file=sys.stderr)
            self._send_empty(502)
            return

        try:
            request_line = f"{self.command} {req_path} HTTP/1.1\r\n"
            header_lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            upstream_sock.sendall((request_line + header_lines + "\r\n").encode("latin-1"))

            resp_buf = b""
            upstream_sock.settimeout(10)
            while b"\r\n\r\n" not in resp_buf:
                chunk = upstream_sock.recv(4096)
                if not chunk:
                    break
                resp_buf += chunk

            header_part, sep, leftover = resp_buf.partition(b"\r\n\r\n")
            if not sep:
                print(f"[app] upstream WebSocket handshake with {host}:{port} closed before completing", file=sys.stderr)
                self._send_empty(502)
                return

            # Relay the handshake response back to the real client verbatim,
            # whatever it was — a successful 101, or the upstream declining
            # to upgrade (an ordinary response, which just ends here).
            self.wfile.write(header_part + b"\r\n\r\n")
            self.wfile.flush()

            status_parts = header_part.split(b"\r\n", 1)[0].split(b" ", 2)
            if len(status_parts) < 2 or status_parts[1] != b"101":
                return

            # Not a fresh HTTP/1.1 request/response from here on — the
            # connection is now a raw, arbitrarily long-lived, bidirectional
            # pipe of WebSocket frames neither side treats as HTTP anymore.
            # The 120s idle timeout _Handler normally applies (to reclaim
            # threads pinned by an abandoned keep-alive connection) doesn't
            # apply to an intentionally long-lived WebSocket session.
            self.connection.settimeout(None)
            upstream_sock.settimeout(None)
            if leftover:
                self.connection.sendall(leftover)
            self._relay_bidirectional(self.connection, upstream_sock)
        except (ConnectionError, OSError) as exc:
            print(f"[app] WebSocket relay to {host}:{port} failed: {exc!r}", file=sys.stderr)
        finally:
            try:
                upstream_sock.close()
            except OSError:
                pass

    def _relay_bidirectional(self, sock_a: "socket.socket", sock_b: "socket.socket") -> None:
        """Shuttle raw bytes between two sockets in both directions until
        either side closes — used for WebSocket after a successful Upgrade,
        where neither side is speaking HTTP anymore."""
        def _pump(src: "socket.socket", dst: "socket.socket") -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=_pump, args=(sock_a, sock_b), daemon=True)
        t.start()
        _pump(sock_b, sock_a)
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# HTTP/2 transport (HTTP20_ENABLED)
# ---------------------------------------------------------------------------

HTTP2_CONNECTION_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


class _Http2StreamHandler(_RoutingMixin):
    """One HTTP/2 stream == one logical request. All actual socket I/O is
    delegated to the shared _Http2Connection, which owns the H2Connection
    state machine and serializes writes across the connection's concurrent
    streams."""

    def __init__(self, conn: "_Http2Connection", stream_id: int,
                 pseudo_headers: "dict[str, str]", headers: "list[tuple[str, str]]", body: bytes) -> None:
        self._conn = conn
        self._stream_id = stream_id
        self._body = body
        self.path = pseudo_headers.get(":path", "/")
        self.command = pseudo_headers.get(":method", "GET")
        self._scheme_value = pseudo_headers.get(":scheme", "http")
        # HTTP/2 clients carry the request authority in the :authority
        # pseudo-header instead of a literal Host header (RFC 7540
        # 8.1.2.3), so genuine HTTP/2 clients send no Host at all. Without
        # this, _proxy_to's downstream relay (which just forwards
        # _all_headers() verbatim) would silently drop the request's
        # origin, and Python's http.client would substitute the upstream's
        # own host:port instead — visible as oauth2-proxy building its IdP
        # redirect_uri from its own internal loopback address rather than
        # the gateway's public one.
        authority = pseudo_headers.get(":authority", "")
        if authority and not any(hname.lower() == "host" for hname, _ in headers):
            headers = [("Host", authority)] + list(headers)
        self._headers = headers

    # --- _RoutingMixin primitives ---

    def _header(self, name: str) -> str:
        lname = name.lower()
        for hname, hvalue in self._headers:
            if hname.lower() == lname:
                return hvalue
        return ""

    def _all_headers(self) -> "list[tuple[str, str]]":
        return list(self._headers)

    def _peer_ip(self) -> str:
        return self._conn.peer_ip

    def _scheme(self) -> str:
        return self._scheme_value or "http"

    def _read_request_body(self) -> "bytes | None":
        return self._body or None

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self._send_response(status, [("content-type", "text/plain; charset=utf-8")], body)

    def _send_html(self, markup: str, status: int = 200) -> None:
        body = markup.encode("utf-8")
        self._send_response(status, [
            ("content-type", "text/html; charset=utf-8"),
            ("cache-control", "no-store"),
        ], body)

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_response(status, [
            ("content-type", "application/json; charset=utf-8"),
            ("cache-control", "no-store"),
        ], body)

    def _send_empty(self, status: int, extra_headers: "dict[str, str] | None" = None) -> None:
        self._send_response(status, list((extra_headers or {}).items()), b"")

    def _redirect(self, url: str, status: int = 302,
                   cookies: "list[str] | None" = None) -> None:
        headers = [("location", url)] + [("set-cookie", cookie) for cookie in (cookies or [])]
        self._send_response(status, headers, b"")

    def _send_raw_response(self, status: int, headers: "list[tuple[str, str]]", body: bytes) -> None:
        # Content-Length is redundant in HTTP/2 (framing comes from the DATA
        # frame boundaries, not a header) but harmless to pass through.
        self._send_response(status, headers, body)

    def _send_response_with_trailers(self, status: int, headers: "list[tuple[str, str]]",
                                       body: bytes, trailers: "list[tuple[str, str]]") -> None:
        self._conn.send_stream_response(self._stream_id, status, headers, body, trailers=trailers or None)

    def _send_response(self, status: int, headers: "list[tuple[str, str]]", body: bytes) -> None:
        self._conn.send_stream_response(self._stream_id, status, headers, body)

    def _stream_response(self, status: int, headers: "list[tuple[str, str]]", chunks) -> None:
        # HTTP/2 frames data incrementally natively (no Content-Length or
        # chunked-encoding trick needed) — just send each chunk as its own
        # DATA frame and end the stream once the upstream response does.
        self._conn.send_stream_headers(self._stream_id, status, headers)
        try:
            for chunk in chunks:
                if chunk:
                    self._conn.send_stream_data(self._stream_id, chunk)
        except Exception:
            pass
        finally:
            self._conn.end_stream(self._stream_id)

    def _stream_response_with_trailers(self, status: int, headers: "list[tuple[str, str]]",
                                        chunks, get_trailers) -> None:
        self._conn.send_stream_headers(self._stream_id, status, headers)
        try:
            for chunk in chunks:
                if chunk:
                    self._conn.send_stream_data(self._stream_id, chunk)
        except Exception:
            pass
        trailers = get_trailers()
        if trailers:
            self._conn.send_stream_trailers(self._stream_id, trailers)
        else:
            self._conn.end_stream(self._stream_id)


class _Http2Connection:
    """Owns one TCP connection's H2Connection state machine. run() executes
    the read loop on the calling thread; each fully-received request (stream)
    is dispatched to its own thread so a slow handler (e.g. the auth check's
    outbound HTTP call) doesn't block other concurrent streams on the same
    connection. Response bodies are sent as a single DATA frame — fine for
    ordinary Easy Auth responses (JSON/HTML/redirects), which _proxy_to also
    already buffers fully in memory; a response larger than the default flow
    control window would need proper windowed sending, not implemented here.
    """

    def __init__(self, sock: "socket.socket", force_relay_http2: bool = False) -> None:
        self._sock = sock
        self._force_relay_http2 = force_relay_http2
        self._conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
        self._write_lock = threading.RLock()
        self._streams: "dict[int, dict]" = {}
        try:
            self.peer_ip = sock.getpeername()[0]
        except OSError:
            self.peer_ip = ""

    def run(self) -> None:
        self._conn.initiate_connection()
        self._flush()
        try:
            while True:
                try:
                    data = self._sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    events = self._conn.receive_data(data)
                except h2.exceptions.ProtocolError:
                    return
                closed = False
                for event in events:
                    if isinstance(event, h2.events.ConnectionTerminated):
                        closed = True
                    else:
                        self._handle_event(event)
                self._flush()
                if closed:
                    return
        finally:
            try:
                self._sock.close()
            except OSError:
                pass

    def _handle_event(self, event) -> None:
        if isinstance(event, h2.events.RequestReceived):
            self._streams[event.stream_id] = {"headers": event.headers, "body": bytearray()}
        elif isinstance(event, h2.events.DataReceived):
            stream = self._streams.get(event.stream_id)
            if stream is not None:
                stream["body"] += event.data
            self._conn.acknowledge_received_data(len(event.data), event.stream_id)
        elif isinstance(event, h2.events.StreamEnded):
            stream = self._streams.pop(event.stream_id, None)
            if stream is not None:
                threading.Thread(
                    target=self._dispatch_stream,
                    args=(event.stream_id, stream["headers"], bytes(stream["body"])),
                    daemon=True,
                ).start()
        elif isinstance(event, h2.events.StreamReset):
            self._streams.pop(event.stream_id, None)

    def _dispatch_stream(self, stream_id: int, raw_headers, body: bytes) -> None:
        pseudo: "dict[str, str]" = {}
        headers: "list[tuple[str, str]]" = []
        for name, value in raw_headers:
            name = name.decode() if isinstance(name, bytes) else name
            value = value.decode() if isinstance(value, bytes) else value
            if name.startswith(":"):
                pseudo[name] = value
            else:
                headers.append((name, value))
        handler = _Http2StreamHandler(self, stream_id, pseudo, headers, body)
        handler._force_http2_upstream = self._force_relay_http2
        try:
            handler._dispatch()
        except Exception:
            try:
                self.send_stream_response(stream_id, 500, [], b"")
            except Exception:
                pass

    def send_stream_response(self, stream_id: int, status: int,
                              headers: "list[tuple[str, str]]", body: bytes,
                              trailers: "list[tuple[str, str]] | None" = None) -> None:
        with self._write_lock:
            response_headers = [(":status", str(status))]
            response_headers.extend((name, value) for name, value in headers if name.lower() != "content-length")
            try:
                self._conn.send_headers(stream_id, response_headers)
                if trailers:
                    self._conn.send_data(stream_id, body, end_stream=False)
                    self._conn.send_headers(stream_id, trailers, end_stream=True)
                else:
                    self._conn.send_data(stream_id, body, end_stream=True)
            except h2.exceptions.StreamClosedError:
                return
            self._flush()

    def send_stream_headers(self, stream_id: int, status: int, headers: "list[tuple[str, str]]") -> None:
        """Start a streamed response: headers only, stream left open for
        send_stream_data calls. Used by _stream_response for SSE/streaming
        upstream responses — HTTP/2 frames data incrementally natively, so
        (unlike the HTTP/1.1 transport) no chunked-encoding trick is needed."""
        with self._write_lock:
            response_headers = [(":status", str(status))]
            response_headers.extend((name, value) for name, value in headers if name.lower() != "content-length")
            try:
                self._conn.send_headers(stream_id, response_headers)
            except h2.exceptions.StreamClosedError:
                return
            self._flush()

    def send_stream_data(self, stream_id: int, data: bytes) -> None:
        with self._write_lock:
            try:
                self._conn.send_data(stream_id, data, end_stream=False)
            except (h2.exceptions.StreamClosedError, h2.exceptions.FlowControlError):
                return
            self._flush()

    def end_stream(self, stream_id: int) -> None:
        with self._write_lock:
            try:
                self._conn.send_data(stream_id, b"", end_stream=True)
            except h2.exceptions.StreamClosedError:
                return
            self._flush()

    def send_stream_trailers(self, stream_id: int, trailers: "list[tuple[str, str]]") -> None:
        """Ends the stream with a trailing HEADERS frame (e.g. gRPC's
        grpc-status/grpc-message) instead of an empty terminating DATA
        frame."""
        with self._write_lock:
            try:
                self._conn.send_headers(stream_id, trailers, end_stream=True)
            except h2.exceptions.StreamClosedError:
                return
            self._flush()

    def _flush(self) -> None:
        with self._write_lock:
            data = self._conn.data_to_send()
            if data:
                try:
                    self._sock.sendall(data)
                except OSError:
                    pass


_H2_REQUEST_PSEUDO_HEADERS = frozenset({":method", ":path", ":scheme", ":authority"})


def _http2_relay_request(host: str, port: int, method: str, path: str,
                          headers: "dict[str, str]", body: "bytes | None",
                          tls: bool = False):
    """Relay one request to APP_UPSTREAM over a fresh HTTP/2 connection —
    plaintext (h2c) or, when tls=True, TLS with ALPN "h2" negotiation
    (required by upstreams like nginx that only ever speak HTTP/2 over TLS).
    Used by _proxy_to for HTTP20_PROXY_MODE "all"/"grpc-only". The request
    body (if any) is still sent as a single DATA frame (not a general
    bidirectional-streaming gRPC client) but the response is a generator,
    yielding it to the caller incrementally rather than buffering it all in
    memory first — this is what lets a streaming (e.g. SSE) endpoint work
    under HTTP20_PROXY_MODE=all instead of hanging until it completes.

    Yields, in order: ("status", status, resp_headers) exactly once, then
    ("data", chunk) for each DATA frame as it arrives, then finally
    ("trailers", resp_trailers) once the stream ends (resp_trailers may be
    an empty list). Raises ConnectionError if no response is ever received —
    always before the first yield, so the caller can still send a clean 502
    in that case; once ("status", ...) has been yielded, headers are assumed
    committed to the real client and this generator only ever ends quietly."""
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    sock = socket.create_connection((host, port), timeout=30)
    try:
        if tls:
            sock = _UPSTREAM_SSL_CONTEXT_H2.wrap_socket(sock, server_hostname=host)
            if sock.selected_alpn_protocol() != "h2":
                raise ConnectionError(f"upstream did not negotiate HTTP/2 over TLS (got {sock.selected_alpn_protocol()!r})")
        sock.sendall(conn.data_to_send())

        stream_id = conn.get_next_available_stream_id()
        request_headers = [
            (":method", method),
            (":path", path),
            (":scheme", "https" if tls else "http"),
            (":authority", f"{host}:{port}"),
        ]
        for name, value in headers.items():
            if name.lower() in _H2_REQUEST_PSEUDO_HEADERS or name.lower() == "host":
                continue
            request_headers.append((name.lower(), value))
        conn.send_headers(stream_id, request_headers, end_stream=body is None)
        sock.sendall(conn.data_to_send())
        if body is not None:
            conn.send_data(stream_id, body, end_stream=True)
            sock.sendall(conn.data_to_send())

        resp_trailers: "list[tuple[str, str]]" = []
        got_response = False
        abort_reason = ""
        done = False
        while not done:
            data = sock.recv(65536)
            if not data:
                break
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    got_response = True
                    status = 502
                    resp_headers: "list[tuple[str, str]]" = []
                    for name, value in event.headers:
                        name = name.decode() if isinstance(name, bytes) else name
                        value = value.decode() if isinstance(value, bytes) else value
                        if name == ":status":
                            status = int(value)
                        else:
                            resp_headers.append((name, value))
                    yield ("status", status, resp_headers)
                elif isinstance(event, h2.events.DataReceived):
                    conn.acknowledge_received_data(len(event.data), event.stream_id)
                    if event.data:
                        yield ("data", event.data)
                elif isinstance(event, h2.events.TrailersReceived):
                    for name, value in event.headers:
                        name = name.decode() if isinstance(name, bytes) else name
                        value = value.decode() if isinstance(value, bytes) else value
                        resp_trailers.append((name, value))
                elif isinstance(event, h2.events.StreamEnded):
                    done = True
                elif isinstance(event, h2.events.StreamReset):
                    abort_reason = f"upstream reset the stream (error_code={event.error_code!r})"
                    done = True
                elif isinstance(event, h2.events.ConnectionTerminated):
                    abort_reason = abort_reason or f"upstream closed the connection (error_code={event.error_code!r}, additional_data={event.additional_data!r})"
                    done = True
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
        if not got_response:
            reason = abort_reason or "connection closed with no data"
            raise ConnectionError(f"no HTTP/2 response received from {host}:{port} ({reason})")
        yield ("trailers", resp_trailers)
    finally:
        sock.close()


def _looks_like_http2_preface(sock: "socket.socket") -> bool:
    """Peek (without consuming) the start of a plaintext connection to tell
    an h2c client's connection preface apart from an ordinary HTTP/1.1
    request line. Only meaningful for non-TLS sockets — TLS connections
    negotiate the protocol via ALPN instead (checked by the caller)."""
    try:
        sock.settimeout(0.5)
        peeked = sock.recv(len(HTTP2_CONNECTION_PREFACE), socket.MSG_PEEK)
    except OSError:
        return False
    finally:
        sock.settimeout(None)
    return len(peeked) > 0 and HTTP2_CONNECTION_PREFACE.startswith(peeked)


class _MultiplexingServer(ThreadingHTTPServer):
    """Accepts both HTTP/1.1 and HTTP/2 on the same SITE_PORT (when
    HTTP20_ENABLED) — mirrors App Service's "HTTP version: 2.0" being additive
    to, not exclusive of, HTTP/1.1. Peeks at each new connection (or checks
    the negotiated ALPN protocol for TLS) before constructing a handler,
    since h2 cannot itself speak HTTP/1.1 and BaseHTTPRequestHandler cannot
    itself speak HTTP/2."""

    def finish_request(self, request, client_address) -> None:
        if HTTP20_ENABLED:
            if isinstance(request, ssl.SSLSocket):
                is_http2 = request.selected_alpn_protocol() == "h2"
            else:
                is_http2 = _looks_like_http2_preface(request)
            if is_http2:
                _Http2Connection(request).run()
                return
        _Handler(request, client_address, self)


class _Http2OnlyServer(ThreadingHTTPServer):
    """APPSERVICE_HTTP20_ONLY_PORT listener — mirrors Azure App Service's
    dedicated gRPC port: every connection here is HTTP/2 (h2c, or TLS
    restricted to ALPN "h2"), unconditionally. No protocol sniffing needed
    (unlike _MultiplexingServer) since this port never speaks HTTP/1.1."""

    def finish_request(self, request, client_address) -> None:
        _Http2Connection(request, force_relay_http2=True).run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    port = int(SITE_PORT)
    if APPSERVICE_HTTP20_ONLY_PORT == port:
        print(
            f"[app] ERROR: APPSERVICE_HTTP20_ONLY_PORT ({APPSERVICE_HTTP20_ONLY_PORT}) "
            f"must differ from SITE_PORT ({port})",
            file=sys.stderr,
        )
        sys.exit(1)
    if sys.platform == "win32":
        # On Windows, SO_REUSEADDR lets a second process bind an in-use port,
        # silently splitting requests between two gateway instances (e.g. a
        # stale emulator left over from a crashed session) — fail fast instead.
        _MultiplexingServer.allow_reuse_address = False
        _Http2OnlyServer.allow_reuse_address = False
    try:
        server = _MultiplexingServer(("0.0.0.0", port), _Handler)
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
        if HTTP20_ENABLED:
            ctx.set_alpn_protocols(["h2", "http/1.1"])
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"[app] Listening on https://0.0.0.0:{port}" + (" (HTTP/2 enabled)" if HTTP20_ENABLED else ""))
    else:
        print(f"[app] Listening on http://0.0.0.0:{port}" + (" (HTTP/2 enabled)" if HTTP20_ENABLED else ""))

    if APPSERVICE_HTTP20_ONLY_PORT:
        try:
            dedicated_server = _Http2OnlyServer(("0.0.0.0", APPSERVICE_HTTP20_ONLY_PORT), _Handler)
        except OSError as exc:
            print(
                f"[app] ERROR: cannot listen on port {APPSERVICE_HTTP20_ONLY_PORT} "
                f"(APPSERVICE_HTTP20_ONLY_PORT): {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        if _TLS_ENABLED:
            dedicated_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                dedicated_ctx.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
            except Exception as exc:
                print(f"[app] ERROR: Failed to load TLS certificate: {exc}", file=sys.stderr)
                sys.exit(1)
            dedicated_ctx.set_alpn_protocols(["h2"])
            dedicated_server.socket = dedicated_ctx.wrap_socket(dedicated_server.socket, server_side=True)
            print(f"[app] Listening on https://0.0.0.0:{APPSERVICE_HTTP20_ONLY_PORT} (HTTP/2 only, mirrors App Service's HTTP20_ONLY_PORT)")
        else:
            print(f"[app] Listening on http://0.0.0.0:{APPSERVICE_HTTP20_ONLY_PORT} (HTTP/2 only, mirrors App Service's HTTP20_ONLY_PORT)")
        threading.Thread(target=dedicated_server.serve_forever, daemon=True).start()

    server.serve_forever()


if __name__ == "__main__":
    main()

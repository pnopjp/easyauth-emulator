"""
Shared logic between src/sample_app.py (distributed with the emulator's own
binary) and tests/protocol/app.py (dev-only, adds a gRPC service on top) —
both play the same role: a demo/verification "real app" run behind
APP_UPSTREAM to see Easy Auth and various protocol behaviors end to end.
Split out here so neither has to duplicate the principal/claims/storage
report logic or the WebSocket/SSE/chunked-body demo handlers.

Config loading works the same way for both callers regardless of where they
live on disk: __file__ below always resolves to this module's own path
(src/_sample_app_shared.py), so runtime_root ends up at the repo root either
way when running from source. --config (also parsed here) lets a caller
point at a different file entirely - tests/python/test_protocol_gaps.py's
protocol_app fixture uses this to avoid tests.protocol.app picking up a
developer's real, secret-bearing config.toml.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import socket
import sys
import time
import tomllib
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import h2.config
import h2.connection
import h2.events
import h2.exceptions


def _parse_config_arg() -> "Path | None":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=str, default=None)
    known, _ = parser.parse_known_args()
    return Path(known.config) if known.config else None


def _load_config() -> dict[str, str]:
    override = _parse_config_arg()
    if override is not None:
        config_path = override
    else:
        runtime_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
        config_path = runtime_root / "config.toml"
        if not config_path.exists() and getattr(sys, "frozen", False):
            config_path = Path(__file__).resolve().parent.parent / "config.toml"
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


_CONFIG = _load_config()


def _cfg(key: str, default: str = "") -> str:
    # Env var wins over config.toml, matching src/app.py's own _cfg — this is
    # what lets tests/python/test_protocol_gaps.py's protocol_app fixture
    # assign a fresh SAMPLE_APP_PORT per test run via env var, same as it
    # already does for the gateway itself.
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    return _CONFIG.get(key, default)


# ---------------------------------------------------------------------------
# Principal / claims / storage verification (formerly all of sample_app.py)
# ---------------------------------------------------------------------------

APP_TITLE = _cfg("SAMPLE_APP_TITLE", "Easy Auth verification app")
APP_DESCRIPTION = _cfg(
    "SAMPLE_APP_DESCRIPTION",
    "A separate test web application used to validate authentication, claims, groups, logout, and optional Storage access.",
)
STORAGE_BLOB_URL = (_cfg("SAMPLE_APP_STORAGE_BLOB_URL") or "").strip()
STORAGE_TIMEOUT_SECONDS = float(_cfg("SAMPLE_APP_STORAGE_TIMEOUT_SECONDS", "10") or "10")
STORAGE_PREVIEW_BYTES = int(_cfg("SAMPLE_APP_STORAGE_PREVIEW_BYTES", "4096") or "4096")
IDP_ENTRA_OIDC_ISSUER_URL = (_cfg("IDP_ENTRA_OIDC_ISSUER_URL") or "").strip()
IDP_ENTRA_CLIENT_ID = (_cfg("IDP_ENTRA_CLIENT_ID") or "").strip()
IDP_ENTRA_CLIENT_SECRET = (_cfg("IDP_ENTRA_CLIENT_SECRET") or "").strip()
OBO_STORAGE_SCOPE = (_cfg("SAMPLE_APP_OBO_STORAGE_SCOPE", "https://storage.azure.com/.default") or "").strip()

EASYAUTH_HEADER_NAMES = [
    "X-Forwarded-User",
    "X-Forwarded-Email",
    "X-Easyauth-User-Id-Claim",
    "X-MS-CLIENT-PRINCIPAL-NAME",
    "X-MS-CLIENT-PRINCIPAL",
    "X-MS-CLIENT-PRINCIPAL-ID",
    "X-MS-CLIENT-PRINCIPAL-IDP",
    "X-MS-TOKEN-AAD-ACCESS-TOKEN",
    "X-MS-TOKEN-AAD-ID-TOKEN",
]

SENSITIVE_EASYAUTH_HEADERS = {
    "X-MS-CLIENT-PRINCIPAL",
    "X-MS-CLIENT-PRINCIPAL-ID",
    "X-MS-TOKEN-AAD-ACCESS-TOKEN",
    "X-MS-TOKEN-AAD-ID-TOKEN",
}


def _resolve_storage_blob_url() -> str:
    if not STORAGE_BLOB_URL:
        return ""

    parsed = urlsplit(STORAGE_BLOB_URL)
    path = parsed.path.lstrip("/")
    if not parsed.scheme or not parsed.netloc or "/" not in path:
        return ""

    return STORAGE_BLOB_URL


RESOLVED_STORAGE_BLOB_URL = _resolve_storage_blob_url()

_MAX_ERROR_PREVIEW_LEN = 1200
_AZURE_STORAGE_API_VERSION = "2023-11-03"
_MAX_CLAIM_DISPLAY_LEN = 80
_BASE64_LIKE_RE = re.compile(r'^[A-Za-z0-9+/=_-]+$')


def _header(headers: dict[str, str], name: str) -> str:
    # HTTP header names are case-insensitive (RFC 7230 §3.2), and HTTP/2 in
    # particular mandates lowercase names on the wire (RFC 7540 §8.1.2) — the
    # gateway's own HTTP/2 upstream relay already lowercases everything it
    # sends, so a plain case-sensitive dict.get() against EASYAUTH_HEADER_NAMES'
    # mixed-case names (e.g. "X-MS-CLIENT-PRINCIPAL") silently misses under
    # HTTP20_PROXY_MODE="all"/"grpc-only", even though the exact same headers
    # arrive fine over HTTP/1.1 (which happens to preserve whatever case the
    # sender used, matching these names by coincidence, not by contract).
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return ""


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) < 6:
        return "***MASKED***"
    return f"{value[:3]}***MASKED***{value[-3:]}"


def _easyauth_headers(headers: dict[str, str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for name in EASYAUTH_HEADER_NAMES:
        raw = _header(headers, name)
        masked = name in SENSITIVE_EASYAUTH_HEADERS and bool(raw)
        values.append(
            {
                "name": name,
                "value": _mask_secret(raw) if masked else raw,
                "present": bool(raw),
                "masked": masked,
            }
        )
    return values


def _decode_client_principal(raw_value: str) -> "dict[str, Any] | None":
    if not raw_value:
        return None

    try:
        decoded = base64.b64decode(raw_value).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def _decode_jwt_payload(raw_token: str) -> "dict[str, Any] | None":
    token = (raw_token or "").strip()
    if not token:
        return None
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    parts = token.split(".")
    if len(parts) != 3:
        return None

    payload_part = parts[1]
    padding = "=" * (-len(payload_part) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode((payload_part + padding).encode("ascii"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def _assertion_claims_summary(raw_token: str) -> "dict[str, Any] | None":
    claims = _decode_jwt_payload(raw_token)
    if not claims:
        return None

    summary: dict[str, Any] = {}
    for key in ("aud", "appid", "azp", "iss", "tid", "ver", "scp", "roles"):
        value = claims.get(key)
        if value is not None:
            summary[key] = value
    return summary or None


def _claims_from_principal(principal: "dict[str, Any] | None") -> list[dict[str, str]]:
    if not principal:
        return []

    claims = principal.get("claims")
    if not isinstance(claims, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in claims:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("typ", "")).strip()
        val = str(item.get("val", "")).strip()
        if typ and val:
            normalized.append({"typ": typ, "val": val})
    return normalized


def _group_claims(claims: list[dict[str, str]]) -> list[str]:
    values: list[str] = []
    for claim in claims:
        if claim["typ"].lower() in {"groups", "roles"}:
            values.append(claim["val"])
    return values


def principal_summary(headers: dict[str, str]) -> dict[str, Any]:
    principal = _decode_client_principal(_header(headers, "X-MS-CLIENT-PRINCIPAL"))
    claims = _claims_from_principal(principal)
    groups = _group_claims(claims)
    provider = _header(headers, "X-MS-CLIENT-PRINCIPAL-IDP") or "unknown"
    access_token_present = bool(_header(headers, "X-MS-TOKEN-AAD-ACCESS-TOKEN"))
    id_token_raw = _header(headers, "X-MS-TOKEN-AAD-ID-TOKEN")
    id_token_present = bool(id_token_raw)
    id_token_claims = _decode_jwt_payload(id_token_raw) if id_token_raw else None
    user = _header(headers, "X-Forwarded-User") or _header(headers, "X-MS-CLIENT-PRINCIPAL-NAME")
    email = _header(headers, "X-Forwarded-Email") or _header(headers, "X-MS-CLIENT-PRINCIPAL-NAME")
    user_id_claim = _header(headers, "X-Easyauth-User-Id-Claim") or "preferred_username"

    return {
        "authenticated": bool(user or email),
        "provider": provider,
        "user": user or "",
        "email": email or "",
        "user_id_claim": user_id_claim,
        "client_principal": principal,
        "claims": claims,
        "group_claims": groups,
        "access_token_present": access_token_present,
        "id_token_present": id_token_present,
        "id_token_claims": id_token_claims,
        "easyauth_headers": _easyauth_headers(headers),
    }


def _resolve_tenant_id() -> str:
    if not IDP_ENTRA_OIDC_ISSUER_URL:
        return ""

    parsed = urlsplit(IDP_ENTRA_OIDC_ISSUER_URL)
    segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if not segments:
        return ""

    return segments[0]


def _resolve_client_secret() -> "tuple[str, str]":
    if not IDP_ENTRA_CLIENT_SECRET:
        return "", "missing"
    return IDP_ENTRA_CLIENT_SECRET, "config"


def _read_http_error_body(error: HTTPError) -> str:
    body = error.read()
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(body).decode("ascii")


def _acquire_storage_token_via_obo(user_access_token: str) -> dict[str, Any]:
    tenant_id = _resolve_tenant_id()
    client_secret, secret_source = _resolve_client_secret()
    if not tenant_id or not IDP_ENTRA_CLIENT_ID or not client_secret:
        config_hint = "Set IDP_ENTRA_OIDC_ISSUER_URL, IDP_ENTRA_CLIENT_ID, and IDP_ENTRA_CLIENT_SECRET to enable delegated Storage access."
        return {
            "ok": False,
            "status": "obo-not-configured",
            "message": config_hint,
            "secret_source": secret_source,
        }

    token_url = f"https://login.microsoftonline.com/{quote(tenant_id, safe='')}/oauth2/v2.0/token"
    body = urlencode(
        {
            "client_id": IDP_ENTRA_CLIENT_ID,
            "client_secret": client_secret,
            "scope": OBO_STORAGE_SCOPE,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "requested_token_use": "on_behalf_of",
            "assertion": user_access_token,
        }
    ).encode("utf-8")

    request = Request(
        token_url,
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urlopen(request, timeout=STORAGE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return {
                    "ok": False,
                    "status": "obo-invalid-response",
                    "message": "Token endpoint response did not include a usable access_token.",
                }
            return {
                "ok": True,
                "status": "ok",
                "access_token": access_token,
                "scope": payload.get("scope", OBO_STORAGE_SCOPE),
                "token_type": payload.get("token_type", "Bearer"),
                "secret_source": secret_source,
            }
    except HTTPError as error:
        return {
            "ok": False,
            "status": "obo-http-error",
            "http_status": error.code,
            "message": "OBO token exchange failed.",
            "error": _read_http_error_body(error)[:_MAX_ERROR_PREVIEW_LEN],
            "secret_source": secret_source,
        }
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        return {
            "ok": False,
            "status": "obo-error",
            "message": "Failed to acquire an OBO access token.",
            "error": str(error),
            "secret_source": secret_source,
        }


def _access_blob(token: str, token_source: str, scope: str) -> dict[str, Any]:
    blob_url = RESOLVED_STORAGE_BLOB_URL
    message_ok = (
        "Storage data was fetched with a delegated OBO token."
        if token_source == "obo"
        else "Storage data was fetched with the forwarded access token directly."
    )
    request = Request(
        blob_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Range": f"bytes=0-{max(STORAGE_PREVIEW_BYTES - 1, 0)}",
            "x-ms-version": _AZURE_STORAGE_API_VERSION,
            "x-ms-date": dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            "Accept": "application/octet-stream",
        },
    )

    try:
        with urlopen(request, timeout=STORAGE_TIMEOUT_SECONDS) as response:
            raw = response.read(STORAGE_PREVIEW_BYTES)
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            charset = response.headers.get_content_charset() or "utf-8"

            try:
                preview_text = raw.decode(charset)
                preview_kind = "text"
            except UnicodeDecodeError:
                preview_text = base64.b64encode(raw).decode("ascii")
                preview_kind = "base64"

            return {
                "enabled": True,
                "status": "ok",
                "message": message_ok,
                "blob_url": blob_url,
                "http_status": getattr(response, "status", 200),
                "content_type": content_type,
                "preview_kind": preview_kind,
                "preview": preview_text,
                "token_source": token_source,
                "scope": scope,
            }
    except HTTPError as error:
        return {
            "enabled": True,
            "status": "http-error",
            "message": "Azure Storage rejected the request.",
            "blob_url": blob_url,
            "http_status": error.code,
            "error": _read_http_error_body(error)[:_MAX_ERROR_PREVIEW_LEN],
            "token_source": token_source,
            "scope": scope,
        }
    except (URLError, TimeoutError, ValueError) as error:
        return {
            "enabled": True,
            "status": "error",
            "message": "Failed to reach Azure Storage.",
            "blob_url": blob_url,
            "error": str(error),
            "token_source": token_source,
            "scope": scope,
        }


def storage_preview(headers: dict[str, str], storage_flow: str = "direct") -> dict[str, Any]:
    if not RESOLVED_STORAGE_BLOB_URL:
        return {
            "enabled": False,
            "status": "not-configured",
            "message": "Set STORAGE_BLOB_URL to enable storage verification.",
        }

    provider = _header(headers, "X-MS-CLIENT-PRINCIPAL-IDP") or ""
    if provider.lower() != "aad":
        return {
            "enabled": True,
            "status": "not-entra",
            "message": "Storage verification is only attempted for Entra ID sign-in in this sample app.",
        }

    token = _header(headers, "X-MS-TOKEN-AAD-ACCESS-TOKEN")
    if not token:
        return {
            "enabled": True,
            "status": "no-token",
            "message": "No forwarded access token was available from the gateway.",
            "token_source": "forwarded-access-token",
        }

    assertion_claims = _assertion_claims_summary(token)

    if storage_flow == "direct":
        result = _access_blob(token, "direct", "")
        if assertion_claims:
            result["assertion_claims"] = assertion_claims
        return result

    obo_result = _acquire_storage_token_via_obo(token)
    if not obo_result.get("ok"):
        return {
            "enabled": True,
            "status": obo_result.get("status", "obo-error"),
            "message": obo_result.get("message", "OBO token exchange failed."),
            "token_source": "obo",
            "scope": OBO_STORAGE_SCOPE,
            "http_status": obo_result.get("http_status"),
            "error": obo_result.get("error", ""),
            "secret_source": obo_result.get("secret_source", "unknown"),
            "assertion_claims": assertion_claims,
        }

    storage_access_token = str(obo_result["access_token"])
    result = _access_blob(storage_access_token, "obo", obo_result.get("scope", OBO_STORAGE_SCOPE))
    result["secret_source"] = obo_result.get("secret_source", "unknown")
    if assertion_claims:
        result["assertion_claims"] = assertion_claims
    return result


def _id_token_claims_rows(claims: "dict[str, Any] | None") -> str:
    if not claims:
        return ""
    rows = ""
    aud = claims.get("aud", "")
    if isinstance(aud, list):
        aud = ", ".join(str(a) for a in aud)
    if aud:
        rows += _kv_row("ID token aud", str(aud), mono=True)
    iss = claims.get("iss", "")
    if iss:
        rows += _kv_row("ID token iss", str(iss), mono=True)
    tid = claims.get("tid", "")
    if tid:
        rows += _kv_row("ID token tid", str(tid), mono=True)
    return rows


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge(text: str, kind: str = "neutral") -> str:
    return f'<span class="badge {kind}">{_escape(text)}</span>'


def _kv_row(label: str, value: Any, mono: bool = False) -> str:
    value_html = _escape(value)
    if mono:
        value_html = f"<code>{value_html}</code>"
    return f"<div class=\"kv-row\"><dt>{_escape(label)}</dt><dd>{value_html}</dd></div>"


def _truncate_claim_val(val: str) -> "tuple[str, bool]":
    if len(val) <= _MAX_CLAIM_DISPLAY_LEN:
        return val, False
    if _BASE64_LIKE_RE.match(val):
        return val[:_MAX_CLAIM_DISPLAY_LEN], True
    return val, False


def _sanitize_principal_for_display(principal: "dict[str, Any] | None") -> "dict[str, Any] | None":
    if not principal:
        return principal
    sanitized = dict(principal)
    claims = sanitized.get("claims")
    if isinstance(claims, list):
        new_claims = []
        for item in claims:
            if not isinstance(item, dict):
                new_claims.append(item)
                continue
            val = str(item.get("val", ""))
            display_val, truncated = _truncate_claim_val(val)
            new_claims.append({**item, "val": f"{display_val}... [{len(val)} chars, truncated]"} if truncated else item)
        sanitized["claims"] = new_claims
    return sanitized


def _render_claims_table(claims: list[dict[str, str]]) -> str:
    if not claims:
        return '<p class="muted">No claims were forwarded.</p>'

    rows = []
    for claim in claims:
        display_val, truncated = _truncate_claim_val(claim['val'])
        if truncated:
            val_html = f"<div class='claim-cell'><code>{_escape(display_val)}...</code><span class='muted trunc-note'> [{len(claim['val'])} chars, truncated]</span></div>"
        else:
            val_html = f"<div class='claim-cell'><code>{_escape(display_val)}</code></div>"
        rows.append(
            f"<tr><td>{_escape(claim['typ'])}</td><td>{val_html}</td></tr>"
        )

    return (
        '<table class="table"><thead><tr><th>Type</th><th>Value</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_headers_table(headers: list[dict[str, Any]]) -> str:
    if not headers:
        return '<p class="muted">No Easy Auth headers are available.</p>'

    rows = []
    for item in headers:
        label = item.get("name", "")
        value = item.get("value", "") or "(missing)"
        status = "masked" if item.get("masked") else ("present" if item.get("present") else "missing")
        rows.append(
            f"<tr><td class='header-name'><code>{_escape(label)}</code></td><td class='header-value'><code>{_escape(value)}</code></td><td class='header-status'>{_escape(status)}</td></tr>"
        )

    return (
        '<table class="table headers-table"><thead><tr><th>Header</th><th>Value</th><th>Status</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_panel(title: str, content: str) -> str:
    return f'<section class="card"><h2>{_escape(title)}</h2>{content}</section>'


def render_html(auth: dict[str, Any], storage: dict[str, Any], extra_quick_action: str = "") -> str:
    auth_state = "Authenticated" if auth["authenticated"] else "Anonymous"
    storage_state = storage["status"].replace("-", " ").title()
    storage_message = storage.get("message", "")
    principal_json = json.dumps(_sanitize_principal_for_display(auth["client_principal"]) or {}, ensure_ascii=False, indent=2)
    storage_json = json.dumps(storage, ensure_ascii=False, indent=2)

    verify_points = [
        "Authentication context is visible after sign-in.",
        "Storage access outcome is visible for Entra ID sessions.",
        "Group and role claims are surfaced when provided by the IdP.",
        "Logout endpoint clears the authenticated browser session.",
    ]

    suggested_checks = [
        "Sign in and confirm Session shows authenticated user, provider, and user ID claim.",
        "Open Raw /.auth/me and verify the selected IdP and principal payload match expectations.",
        "If testing Storage, set STORAGE_BLOB_URL and confirm HTTP status plus preview or error details.",
        "Click Sign out, then revisit / to confirm authentication is required again.",
    ]

    verify_html = "".join(
        f'<li class="check-item"><strong>{_escape(item)}</strong></li>' for item in verify_points
    )

    session_block = "".join(
        [
            _badge(auth_state, "ok" if auth["authenticated"] else "warn"),
            _badge(f"Provider: {auth['provider']}", "neutral"),
            _badge("Access token forwarded", "ok" if auth["access_token_present"] else "warn"),
            _badge("Group claims present", "ok" if auth["group_claims"] else "warn"),
        ]
    )

    storage_badge_kind = "ok" if storage["status"] == "ok" else ("warn" if storage["status"] in {"not-configured", "no-token", "not-entra"} else "bad")
    storage_block = _badge(storage_state, storage_badge_kind)

    storage_preview_text = _escape(storage.get("preview", "(no preview)"))
    storage_error = _escape(storage.get("error", ""))

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{_escape(APP_TITLE)}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f5f7fb;
        --panel: rgba(255, 255, 255, 0.9);
        --text: #10213a;
        --muted: #5a6b85;
        --line: rgba(16, 33, 58, 0.12);
        --brand: #1847d6;
        --brand-strong: #1439ab;
        --success: #0f8f5b;
        --warning: #9a6a00;
        --danger: #b42318;
        --shadow: 0 24px 60px rgba(16, 33, 58, 0.12);
        --radius: 22px;
      }}

      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        font-family: "Segoe UI", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(24, 71, 214, 0.16), transparent 32%),
          radial-gradient(circle at top right, rgba(17, 145, 121, 0.13), transparent 28%),
          linear-gradient(180deg, var(--bg), #ffffff 56%);
      }}

      .wrap {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 56px; }}
      .hero {{
        display: grid;
        gap: 18px;
        grid-template-columns: minmax(0, 1.6fr) minmax(280px, 0.9fr);
        align-items: stretch;
        margin-bottom: 22px;
      }}
      .hero-main, .hero-side, .card {{
        background: var(--panel);
        backdrop-filter: blur(14px);
        border: 1px solid var(--line);
        border-radius: var(--radius);
        box-shadow: var(--shadow);
      }}
      .hero-main {{ padding: 28px; }}
      .hero-side {{ padding: 22px; display: grid; gap: 14px; align-content: start; }}
      h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.6rem); line-height: 1.02; letter-spacing: -0.04em; }}
      .lede {{ margin: 14px 0 0; color: var(--muted); font-size: 1.02rem; max-width: 68ch; }}
      .tags {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
      .tag {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 8px 12px;
        border-radius: 999px;
        background: rgba(24, 71, 214, 0.08);
        color: var(--brand-strong);
        font-size: 0.92rem;
        font-weight: 600;
      }}
      .quick-actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
      .button {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 11px 16px;
        border-radius: 14px;
        text-decoration: none;
        font-weight: 700;
        border: 1px solid transparent;
      }}
      .button.primary {{ background: var(--brand); color: white; }}
      .button.secondary {{ background: rgba(16, 33, 58, 0.03); color: var(--text); border-color: var(--line); }}
      .button.ghost {{ color: var(--brand-strong); border-color: rgba(24, 71, 214, 0.24); background: rgba(24, 71, 214, 0.06); }}
      .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(12, minmax(0, 1fr)); }}
      .card {{ padding: 20px; }}
    .grid > .card {{ grid-column: span 12; }}
    .grid > .card:nth-child(1),
    .grid > .card:nth-child(2) {{ grid-column: span 6; }}
    .grid > .card:nth-child(3) {{ grid-column: span 7; }}
    .grid > .card:nth-child(4) {{ grid-column: span 5; }}
      .span-7 {{ grid-column: span 7; }}
      .span-5 {{ grid-column: span 5; }}
      .span-12 {{ grid-column: span 12; }}
      .section-title {{ margin: 0 0 14px; font-size: 1.05rem; }}
      .status-row {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }}
      .badge {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        border-radius: 999px;
        padding: 7px 12px;
        font-size: 0.86rem;
        font-weight: 700;
      }}
      .ok {{ background: rgba(15, 143, 91, 0.12); color: var(--success); }}
      .warn {{ background: rgba(154, 106, 0, 0.12); color: var(--warning); }}
      .bad {{ background: rgba(180, 35, 24, 0.12); color: var(--danger); }}
      .neutral {{ background: rgba(24, 71, 214, 0.08); color: var(--brand-strong); }}
      .muted {{ color: var(--muted); }}
      .kv {{ display: grid; gap: 10px; margin: 0; }}
      .kv-row {{
        display: grid;
        grid-template-columns: minmax(128px, 0.5fr) minmax(0, 1.5fr);
        gap: 12px;
        padding: 10px 0;
        border-bottom: 1px solid rgba(16, 33, 58, 0.08);
      }}
      .kv-row:last-child {{ border-bottom: 0; }}
      .kv-row dt {{ font-weight: 700; color: var(--muted); }}
      .kv-row dd {{ margin: 0; word-break: break-word; }}
      pre {{
        margin: 0;
        overflow: auto;
        border-radius: 16px;
        padding: 16px;
        background: white;
        border: 1px solid var(--line);
        line-height: 1.6;
        white-space: pre-wrap;
        word-break: break-word;
      }}
      .table {{ width: 100%; border-collapse: collapse; }}
      .table th, .table td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(16, 33, 58, 0.08); vertical-align: top; }}
      .table th {{ color: var(--muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }}
      .claim-cell {{ word-break: break-all; overflow-wrap: anywhere; }}
      .trunc-note {{ font-size: 0.82rem; font-style: italic; }}
            .headers-table {{ table-layout: fixed; }}
            .headers-table th:nth-child(1), .headers-table td.header-name {{ width: 34%; }}
            .headers-table th:nth-child(2), .headers-table td.header-value {{ width: 50%; }}
            .headers-table th:nth-child(3), .headers-table td.header-status {{ width: 16%; }}
            .headers-table td code {{
                display: block;
                white-space: normal;
                word-break: break-all;
                overflow-wrap: anywhere;
            }}
      ul {{ margin: 0; padding-left: 20px; }}
      .check-item {{ margin: 0 0 10px; }}
      .footer-note {{ margin-top: 18px; color: var(--muted); font-size: 0.95rem; }}
      .panel-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .card-lite {{
        padding: 16px;
        border-radius: 16px;
        background: rgba(24, 71, 214, 0.04);
        border: 1px solid rgba(24, 71, 214, 0.14);
      }}
      @media (max-width: 900px) {{
        .hero, .panel-grid, .grid {{ grid-template-columns: 1fr; }}
        .span-7, .span-5, .span-12 {{ grid-column: span 1; }}
                .grid > .card,
                .grid > .card:nth-child(1),
                .grid > .card:nth-child(2),
                .grid > .card:nth-child(3),
                .grid > .card:nth-child(4) {{ grid-column: span 1; }}
        .kv-row {{ grid-template-columns: 1fr; }}
      }}
      @media (max-width: 640px) {{
        .wrap {{ padding: 18px 14px 32px; }}
        .hero-main, .hero-side, .card {{ padding: 18px; }}
        h1 {{ font-size: 2rem; }}
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="hero">
        <div class="hero-main">
          <div class="tag"><strong>Verification</strong> / separate test web app</div>
          <h1>{_escape(APP_TITLE)}</h1>
          <p class="lede">{_escape(APP_DESCRIPTION)}</p>
          <div class="tags">
            <span class="tag">Auth context</span>
            <span class="tag">Group claims</span>
            <span class="tag">Storage preview</span>
            <span class="tag">Logout flow</span>
          </div>
          <div class="quick-actions">
            <a class="button primary" href="/.auth/login">Sign in</a>
            <a class="button secondary" href="/.auth/logout">Sign out</a>
            <a class="button ghost" href="/.auth/me">Raw /.auth/me</a>
            <a class="button ghost" href="/api/report">JSON report</a>
            {extra_quick_action}
          </div>
        </div>
        <div class="hero-side">
          <div>
            <div class="muted" style="font-size:0.9rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">What to verify</div>
            <div style="margin-top:8px;font-size:1.02rem;font-weight:700;">This app sits behind the gateway and shows the identity and data-access path end to end.</div>
          </div>
          <ul>
                        {verify_html}
          </ul>
        </div>
      </div>

      <div class="grid">
        {_render_panel(
            "Session",
            f"<div class='status-row'>{session_block}</div><dl class='kv'>"
            + _kv_row("Status", auth_state)
            + _kv_row("User", auth['user'], mono=True)
            + _kv_row("Email", auth['email'] or "(none)", mono=True)
            + _kv_row("Provider", auth['provider'], mono=True)
            + _kv_row("User ID claim", auth['user_id_claim'], mono=True)
            + _kv_row("Access token", "present" if auth['access_token_present'] else "missing")
            + _kv_row("ID token", "present" if auth['id_token_present'] else "missing")
            + (_id_token_claims_rows(auth.get('id_token_claims')) if auth.get('id_token_claims') else "")
            + _kv_row("Group claims", ", ".join(auth['group_claims']) or "none", mono=True)
            + "</dl>"
        )}

        {_render_panel(
            "Storage preview",
            f"<div class='status-row'>{storage_block}</div><dl class='kv'>"
            + _kv_row("Message", storage_message)
            + _kv_row("Configured", "yes" if storage.get('enabled') else "no")
            + _kv_row("Token flow", storage.get('token_source', '(unknown)'), mono=True)
            + _kv_row("OBO secret source", storage.get('secret_source', '(n/a)'), mono=True)
            + _kv_row("HTTP status", storage.get('http_status', '(n/a)'))
            + _kv_row("Blob URL", storage.get('blob_url', '(none)'))
            + "</dl>"
            + (f"<h3>Preview</h3><pre>{storage_preview_text}</pre>" if storage.get('preview') else "")
            + (f"<h3>Error</h3><pre>{storage_error}</pre>" if storage.get('error') else "")
        )}

        {_render_panel(
            "Decoded client principal",
            f"<div class='status-row'>{_badge('Decoded claims', 'neutral')}</div>"
            + f"<pre>{_escape(principal_json)}</pre>"
            + "<h3>Claims</h3>"
            + _render_claims_table(auth['claims'])
        )}

        {_render_panel(
            "Easy Auth headers",
            _render_headers_table(auth.get('easyauth_headers', []))
            + "<p class='footer-note'>Sensitive headers are shown as first 3 chars + ***MASKED*** + last 3 chars.</p>"
        )}

        {_render_panel(
            "Suggested checks",
            "<div class='panel-grid'>"
            + "".join(f"<div class='card-lite'><strong>{_escape(item)}</strong></div>" for item in suggested_checks)
            + "</div>"
            + "<p class='footer-note'>If storage or group claims are not enabled in your IdP/app registration, the page will explain why instead of failing silently.</p>"
        )}

        {_render_panel(
            "Raw storage payload",
            f"<pre>{_escape(storage_json)}</pre>"
        )}
      </div>
    </div>
  </body>
</html>"""


def build_report(auth: dict[str, Any], storage: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {
            "id": "auth-context",
            "required": True,
            "status": "pass" if auth["authenticated"] else "fail",
            "description": "Authenticated user context is available.",
            "evidence": {
                "authenticated": auth["authenticated"],
                "provider": auth["provider"],
                "user": auth["user"],
                "email": auth["email"],
            },
        },
        {
            "id": "storage-observable",
            "required": True,
            "status": "pass" if bool(storage.get("status") and storage.get("message")) else "fail",
            "description": "Storage verification outcome is observable.",
            "evidence": {
                "enabled": storage.get("enabled"),
                "status": storage.get("status"),
                "message": storage.get("message", ""),
                "http_status": storage.get("http_status"),
            },
        },
        {
            "id": "group-claims",
            "required": False,
            "status": "pass" if bool(auth["group_claims"]) else "warn",
            "description": "Group/role claims are present when emitted by the IdP.",
            "evidence": {
                "count": len(auth["group_claims"]),
                "values": auth["group_claims"],
            },
        },
        {
            "id": "logout-endpoint",
            "required": True,
            "status": "pass",
            "description": "Logout endpoint is available.",
            "evidence": {
                "path": "/.auth/logout",
                "method": "GET",
            },
        },
    ]

    required_checks = [item for item in checks if item["required"]]
    required_passed = sum(1 for item in required_checks if item["status"] == "pass")
    required_total = len(required_checks)

    return {
        "schema_version": "2026-06-06",
        "generated_at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall": {
            "status": "pass" if required_passed == required_total else "fail",
            "required_passed": required_passed,
            "required_total": required_total,
        },
        "checks": checks,
        "auth": auth,
        "storage": storage,
    }


RECOMMENDATIONS = {
    "checks": [
        "Verify the sign-in provider shown by the app matches the configured IdP.",
        "If group claims are expected, confirm your IdP actually emits them into the principal.",
        "Configure STORAGE_BLOB_URL to test delegated storage reads.",
        "Sign out and confirm the next browser visit requires authentication again.",
    ]
}


# ---------------------------------------------------------------------------
# WebSocket / SSE / chunked-body demo (formerly tests/protocol/app.py)
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

SSE_INTERVAL_SECONDS = float(_cfg("PROTOCOL_DEMO_SSE_INTERVAL_SECONDS", "1") or "1")
SSE_EVENT_COUNT = int(_cfg("PROTOCOL_DEMO_SSE_COUNT", "10") or "10")


def _ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def render_protocol_demo_page(http_port: int, extra_sections_html: str = "", back_url: "str | None" = None) -> str:
    back_link_html = f'<a class="back" href="{_escape(back_url)}">&larr; Back</a>' if back_url else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Protocol demo</title>
<style>
  body {{ font-family: "Segoe UI", sans-serif; max-width: 900px; margin: 32px auto; padding: 0 16px; color: #10213a; }}
  h1 {{ margin-bottom: 4px; }}
  .muted {{ color: #5a6b85; }}
  section {{ border: 1px solid rgba(16,33,58,0.14); border-radius: 12px; padding: 16px 20px; margin: 20px 0; }}
  h2 {{ margin-top: 0; }}
  button {{ padding: 8px 14px; border-radius: 8px; border: 1px solid #1847d6; background: #1847d6; color: white; cursor: pointer; }}
  input[type=text] {{ padding: 6px 8px; border-radius: 6px; border: 1px solid rgba(16,33,58,0.24); width: 260px; }}
  pre.log {{ background: #10213a; color: #d9e4ff; padding: 12px; border-radius: 8px; height: 160px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}
  code {{ background: rgba(16,33,58,0.06); padding: 2px 6px; border-radius: 4px; }}
  pre.cmd {{ background: rgba(16,33,58,0.06); padding: 10px 12px; border-radius: 8px; overflow-x: auto; }}
  a.back {{ display: inline-block; margin-bottom: 12px; }}
</style>
</head>
<body>
{back_link_html}
<h1>Protocol demo</h1>
<p class="muted">Load this page directly (this app's own port) to see each feature work, then load it again through the
EasyAuth Emulator gateway (after setting <code>APP_UPSTREAM</code> to this app's HTTP port) to see which features
break when proxied.</p>

<section>
  <h2>WebSocket</h2>
  <button onclick="wsConnect()">Connect</button>
  <button onclick="wsDisconnect()">Disconnect</button>
  <input type="text" id="wsInput" value="hello" />
  <button onclick="wsSend()">Send</button>
  <pre class="log" id="wsLog"></pre>
</section>

<section>
  <h2>SSE / streaming</h2>
  <p class="muted">Expects one event roughly every {SSE_INTERVAL_SECONDS}s. If they all arrive at once at the end,
  the response is being buffered instead of streamed.</p>
  <button onclick="sseStart()">Start stream</button>
  <pre class="log" id="sseLog"></pre>
</section>

<section>
  <h2>Chunked request body</h2>
  <p class="muted">Browsers cannot send a <code>Transfer-Encoding: chunked</code> request body over HTTP/1.1:
  Chrome/Edge require HTTP/2 or HTTP/3 for streaming <code>fetch</code> request bodies and throw
  <code>net::ERR_H2_OR_QUIC_REQUIRED</code> otherwise, and browsers only ever negotiate HTTP/2 via TLS —
  never over plaintext, even though this app also accepts plaintext HTTP/2 (h2c). Use the curl command
  from a terminal instead.</p>
  <pre class="cmd">curl -X POST --no-buffer -H "Transfer-Encoding: chunked" --data-binary "chunked request body test" &lt;base-url&gt;/chunked/echo</pre>
  <p class="muted">Replace <code>&lt;base-url&gt;</code> with <code>{f"http://localhost:{http_port}"}</code> (direct) or the gateway's <code>SITE_URL:SITE_PORT</code> (via proxy).</p>
</section>

{extra_sections_html}

<script>
function log(id, text) {{
  const el = document.getElementById(id);
  el.textContent += text + "\\n";
  el.scrollTop = el.scrollHeight;
}}

let ws = null;
function wsConnect() {{
  const url = location.origin.replace(/^http/, "ws") + "/ws/echo";
  log("wsLog", "connecting to " + url);
  ws = new WebSocket(url);
  ws.onopen = () => log("wsLog", "[open]");
  ws.onmessage = (e) => log("wsLog", "recv: " + e.data);
  ws.onerror = () => log("wsLog", "[error]");
  ws.onclose = () => log("wsLog", "[closed]");
}}
function wsSend() {{
  if (!ws || ws.readyState !== 1) {{ log("wsLog", "[not connected]"); return; }}
  const value = document.getElementById("wsInput").value;
  ws.send(value);
  log("wsLog", "sent: " + value);
}}
function wsDisconnect() {{
  if (!ws) {{ log("wsLog", "[not connected]"); return; }}
  ws.close(1000, "manual disconnect");
}}

function sseStart() {{
  document.getElementById("sseLog").textContent = "";
  const es = new EventSource("/sse/stream");
  const startedAt = performance.now();
  es.onmessage = (e) => log("sseLog", `+${{Math.round(performance.now() - startedAt)}}ms: ${{e.data}}`);
  es.onerror = () => {{ log("sseLog", "[closed/error]"); es.close(); }};
}}

</script>
</body>
</html>"""


class ProtocolDemoMixin:
    """WebSocket echo / SSE stream / chunked-body echo handlers. The
    consuming BaseHTTPRequestHandler subclass must provide its own
    _send_json/_send_text (both sample_app.py and tests/protocol/app.py
    already have their own, with minor header differences that don't matter
    here)."""

    def _handle_ws_echo(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self._send_text("Expected a WebSocket Upgrade request.", status=400)
            return

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept_key(key))
        self.end_headers()

        try:
            while True:
                frame = self._ws_read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    self._ws_write_frame(0x8, payload)
                    break
                if opcode == 0x9:  # ping -> pong
                    self._ws_write_frame(0xA, payload)
                    continue
                if opcode in (0x1, 0x2):  # text/binary -> echo
                    self._ws_write_frame(opcode, payload)
        except (ConnectionError, OSError):
            pass

    def _ws_read_frame(self) -> "tuple[int, bytes] | None":
        header = self.rfile.read(2)
        if len(header) < 2:
            return None
        b1, b2 = header
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(self.rfile.read(2), "big")
        elif length == 127:
            length = int.from_bytes(self.rfile.read(8), "big")
        mask_key = self.rfile.read(4) if masked else b""
        payload = self.rfile.read(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _ws_write_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + length.to_bytes(2, "big")
        else:
            header += bytes([127]) + length.to_bytes(8, "big")
        self.wfile.write(header + payload)
        self.wfile.flush()

    def _handle_sse(self) -> None:
        # Closing the connection when the stream ends (rather than keeping it
        # alive for a next request) lets a client with no Content-Length know
        # where the response ends without waiting on its own read timeout.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for i in range(SSE_EVENT_COUNT):
                chunk = f"data: tick {i} at {time.time():.3f}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
        except (ConnectionError, OSError):
            pass

    def _read_chunked_request_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._read_chunked_body()
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _read_chunked_body(self) -> bytes:
        chunks = []
        while True:
            size_line = self.rfile.readline().strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    def _handle_chunked_echo(self) -> None:
        body = self._read_chunked_request_body()
        try:
            preview = body.decode("utf-8")
        except UnicodeDecodeError:
            preview = base64.b64encode(body).decode("ascii")
        self._send_json({
            "received_bytes": len(body),
            "transfer_encoding": self.headers.get("Transfer-Encoding", "(none)"),
            "content_length_header": self.headers.get("Content-Length", "(none)"),
            "preview": preview,
        })


class QuietErrorThreadingHTTPServer(ThreadingHTTPServer):
    """Replaces the traceback socketserver otherwise prints to stderr when a
    peer resets the connection mid-request (e.g. a browser tab closed, or
    the upstream killed, while a WebSocket session is active) with a single
    quiet line - expected background noise, not worth a full traceback, but
    still worth a trace that it happened."""

    def handle_error(self, request, client_address) -> None:
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            print(f"[app] connection from {client_address} reset (peer disconnected)", file=sys.stderr)
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------------------
# Plain HTTP/2 (h2c) support, alongside HTTP/1.1
# ---------------------------------------------------------------------------
#
# This lets sample_app.py/tests/protocol/app.py stand in for a real Azure
# App Service/Container Apps backend under HTTP20_PROXY_MODE="all" (or
# "grpc-only" for non-gRPC content), which requires APP_UPSTREAM to actually
# speak HTTP/2 — without it, that gateway relay mode 502s against these demo
# apps. Deliberately much simpler than src/app.py's own _Http2Connection/
# _Http2StreamHandler: no proxying, and no genuine bidirectional-streaming
# endpoint of its own (WebSocket here is HTTP/1.1 Upgrade-only, matching
# real Azure App Service — it always downgrades RFC 8441 to a classic
# Upgrade before the backend ever sees it, confirmed via
# tools/azure-poc/azure-websocket-poc — so a demo "app" has no real-Azure
# equivalent to model by understanding RFC 8441 directly). Streams are
# handled one at a time, synchronously, as each finishes arriving; the
# gateway's own upstream relay only ever opens one stream per fresh
# connection anyway, so this has no practical downside for the scenario
# this exists for. TLS is out of scope too — these apps have no TLS support,
# so only plaintext h2c (prior knowledge, no Upgrade dance) is relevant.

HTTP2_CONNECTION_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


def _looks_like_http2_preface(sock: "socket.socket") -> bool:
    """Peek (without consuming) the start of a connection to tell an h2c
    client's connection preface apart from an ordinary HTTP/1.1 request
    line — same technique as src/app.py's own helper of the same name."""
    try:
        sock.settimeout(0.5)
        peeked = sock.recv(len(HTTP2_CONNECTION_PREFACE), socket.MSG_PEEK)
    except OSError:
        return False
    finally:
        sock.settimeout(None)
    return len(peeked) > 0 and HTTP2_CONNECTION_PREFACE.startswith(peeked)


class _Http2ResponseWriter:
    """Stands in for a BaseHTTPRequestHandler's self.wfile — each write()
    becomes its own DATA frame immediately, which is what lets the SSE demo
    endpoint's per-tick write()+flush() loop deliver genuinely incrementally
    over HTTP/2 too, not just HTTP/1.1."""

    def __init__(self, send_data) -> None:
        self._send_data = send_data

    def write(self, data: bytes) -> int:
        if data:
            self._send_data(data)
        return len(data)

    def flush(self) -> None:
        pass


class Http2RequestAdapterMixin:
    """Mixed in ahead of an existing BaseHTTPRequestHandler subclass (e.g.
    `class Http2Handler(Http2RequestAdapterMixin, Handler): pass`) so the
    SAME do_GET/do_POST/_send_json/etc. logic — which only ever touches
    self.path/self.command/self.headers/self.rfile/self.wfile/
    send_response/send_header/end_headers — runs unchanged for an HTTP/2
    stream instead of a real socket-backed HTTP/1.1 connection. Constructed
    once per stream by _Http2ServingConnection, never via the normal
    BaseHTTPRequestHandler socketserver flow, so BaseHTTPRequestHandler's own
    __init__ (which expects a real request socket) is deliberately never
    called."""

    def __init__(self, command: str, path: str, headers: "dict[str, str]", body: bytes,
                 send_headers_frame, send_data, end_stream) -> None:
        self.command = command
        self.path = path
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = _Http2ResponseWriter(send_data)
        self.close_connection = False  # unused over HTTP/2; kept so existing code that sets it doesn't error
        self._send_headers_frame = send_headers_frame
        self._end_stream = end_stream
        self._status = 200
        self._response_headers: "list[tuple[str, str]]" = []
        self._headers_sent = False
        self._dispatch()

    def _dispatch(self) -> None:
        handler = getattr(self, f"do_{self.command}", None)
        try:
            if handler is None:
                self.send_response(501)
                self.end_headers()
            else:
                handler()
        finally:
            if not self._headers_sent:
                # A handler that never sent a response (shouldn't happen
                # with any route today, but don't leave the stream hanging
                # if one ever does) still needs the stream to end.
                self.send_response(500)
                self.end_headers()
            self._end_stream()

    def send_response(self, code: int, message: "str | None" = None) -> None:
        self._status = code

    def send_header(self, name: str, value: str) -> None:
        if name.lower() == "content-length":
            # HTTP/2 has native DATA framing — a declared Content-Length is
            # redundant, and dropping it here matches src/app.py's own
            # send_stream_response for the gateway's HTTP/2 responses.
            return
        self._response_headers.append((name, value))

    def end_headers(self) -> None:
        self._headers_sent = True
        self._send_headers_frame(self._status, self._response_headers)


class _Http2ServingConnection:
    """Owns one h2c connection's H2Connection state machine for one of
    these demo apps (not the gateway — see the module-level comment above
    for why this can be much simpler than src/app.py's own version)."""

    def __init__(self, sock: "socket.socket", handler_cls: type) -> None:
        self._sock = sock
        self._handler_cls = handler_cls
        self._conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
        self._streams: "dict[int, dict]" = {}

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

    def _send_response_data(self, stream_id: int, data: bytes) -> None:
        """Send data respecting HTTP/2 flow control and the negotiated max
        frame size, instead of one send_data call for the whole chunk — a
        single call larger than the current flow-control window (default
        ~64KB) or the max frame size (default ~16KB) raises FlowControlError/
        FrameTooLargeError, and naively swallowing that error would silently
        drop the entire response body. An authenticated demo page (real JWT
        claims/principal JSON embedded in the HTML) is easily big enough to
        hit this — a page rendered for an anonymous visitor usually isn't,
        which is why this went unnoticed until tested with a real login."""
        remaining = data
        while remaining:
            window = min(self._conn.local_flow_control_window(stream_id), self._conn.max_outbound_frame_size)
            if window <= 0:
                try:
                    incoming = self._sock.recv(65536)
                except OSError:
                    return
                if not incoming:
                    return
                try:
                    events = self._conn.receive_data(incoming)
                except h2.exceptions.ProtocolError:
                    return
                for event in events:
                    if not isinstance(event, h2.events.ConnectionTerminated):
                        # Nested re-entry into _dispatch_stream is possible
                        # here if a different stream happens to finish while
                        # we're blocked waiting for window on this one — safe
                        # (each dispatch only touches its own stream_id), just
                        # unusual, and not expected in practice since the
                        # gateway's own relay only ever opens one stream per
                        # connection anyway.
                        self._handle_event(event)
                self._flush()
                continue
            chunk, remaining = remaining[:window], remaining[window:]
            try:
                self._conn.send_data(stream_id, chunk, end_stream=False)
            except h2.exceptions.StreamClosedError:
                return
            except h2.exceptions.FlowControlError:
                remaining = chunk + remaining
                continue
            self._flush()

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
                self._dispatch_stream(event.stream_id, stream["headers"], bytes(stream["body"]))
        elif isinstance(event, h2.events.StreamReset):
            self._streams.pop(event.stream_id, None)

    def _dispatch_stream(self, stream_id: int, raw_headers, body: bytes) -> None:
        pseudo: "dict[str, str]" = {}
        headers: "dict[str, str]" = {}
        for name, value in raw_headers:
            name = name.decode() if isinstance(name, bytes) else name
            value = value.decode() if isinstance(value, bytes) else value
            if name.startswith(":"):
                pseudo[name] = value
            else:
                headers[name] = value
        # HTTP/2 has no Transfer-Encoding/Content-Length concept of its own
        # (framing comes from DATA/END_STREAM) — synthesize an accurate
        # Content-Length from what was actually buffered so handlers written
        # against HTTP/1.1 semantics (e.g. _read_chunked_request_body) still
        # work correctly regardless of what, if anything, the client declared.
        headers["Content-Length"] = str(len(body))
        method = pseudo.get(":method", "GET")
        path = pseudo.get(":path", "/")

        def send_headers_frame(status: int, resp_headers: "list[tuple[str, str]]") -> None:
            try:
                self._conn.send_headers(stream_id, [(":status", str(status))] + resp_headers)
            except h2.exceptions.StreamClosedError:
                return
            self._flush()

        def send_data(chunk: bytes) -> None:
            self._send_response_data(stream_id, chunk)

        def end_stream() -> None:
            try:
                self._conn.send_data(stream_id, b"", end_stream=True)
            except h2.exceptions.StreamClosedError:
                return
            self._flush()

        try:
            self._handler_cls(method, path, headers, body, send_headers_frame, send_data, end_stream)
        except Exception:
            try:
                self._conn.send_headers(stream_id, [(":status", "500")], end_stream=True)
                self._flush()
            except Exception:
                pass

    def _flush(self) -> None:
        data = self._conn.data_to_send()
        if data:
            try:
                self._sock.sendall(data)
            except OSError:
                pass


class QuietErrorMultiplexingHTTPServer(QuietErrorThreadingHTTPServer):
    """Accepts both HTTP/1.1 and plaintext HTTP/2 (h2c) on the same port —
    mirrors src/app.py's own _MultiplexingServer, minus the TLS/ALPN branch
    (these demo apps have no TLS support). Peeks each new connection's first
    bytes for the h2c client preface before constructing a handler, since h2
    cannot itself speak HTTP/1.1 and BaseHTTPRequestHandler cannot itself
    speak HTTP/2."""

    def __init__(self, server_address, handler_cls: type, http2_handler_cls: type) -> None:
        self._http2_handler_cls = http2_handler_cls
        super().__init__(server_address, handler_cls)

    def finish_request(self, request, client_address) -> None:
        if _looks_like_http2_preface(request):
            _Http2ServingConnection(request, self._http2_handler_cls).run()
            return
        super().finish_request(request, client_address)

from __future__ import annotations

import base64
import binascii
import datetime as dt
import html
import json
import re
import sys
import tomllib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen


def _load_config() -> dict[str, str]:
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
    return _CONFIG.get(key, default)


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
    return headers.get(name, "")


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


def _decode_client_principal(raw_value: str) -> dict[str, Any] | None:
    if not raw_value:
        return None

    try:
        decoded = base64.b64decode(raw_value).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def _decode_jwt_payload(raw_token: str) -> dict[str, Any] | None:
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


def _assertion_claims_summary(raw_token: str) -> dict[str, Any] | None:
    claims = _decode_jwt_payload(raw_token)
    if not claims:
        return None

    summary: dict[str, Any] = {}
    for key in ("aud", "appid", "azp", "iss", "tid", "ver", "scp", "roles"):
        value = claims.get(key)
        if value is not None:
            summary[key] = value
    return summary or None


def _claims_from_principal(principal: dict[str, Any] | None) -> list[dict[str, str]]:
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


def _principal_summary(headers: dict[str, str]) -> dict[str, Any]:
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


def _resolve_client_secret() -> tuple[str, str]:
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


def _storage_preview(headers: dict[str, str], storage_flow: str = "direct") -> dict[str, Any]:
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


def _truncate_claim_val(val: str) -> tuple[str, bool]:
    if len(val) <= _MAX_CLAIM_DISPLAY_LEN:
        return val, False
    if _BASE64_LIKE_RE.match(val):
        return val[:_MAX_CLAIM_DISPLAY_LEN], True
    return val, False


def _sanitize_principal_for_display(principal: dict[str, Any] | None) -> dict[str, Any] | None:
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


def _render_html(auth: dict[str, Any], storage: dict[str, Any]) -> str:
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

    storage_preview = _escape(storage.get("preview", "(no preview)"))
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
            + (f"<h3>Preview</h3><pre>{storage_preview}</pre>" if storage.get('preview') else "")
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


def _build_report(auth: dict[str, Any], storage: dict[str, Any]) -> dict[str, Any]:
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


class Handler(BaseHTTPRequestHandler):
    server_version = "EasyAuthVerificationApp/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self) -> dict[str, str]:
        return {key: value for key, value in self.headers.items()}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=False)
        storage_flow = qs.get("storage_flow", ["direct"])[0].lower()
        if storage_flow not in {"obo", "direct"}:
            storage_flow = "obo"

        if path in {"/", "/index.html"}:
            auth = _principal_summary(self._headers())
            storage = _storage_preview(self._headers(), storage_flow)
            self._send_html(_render_html(auth, storage))
            return

        if path == "/healthz":
            self._send_text("ok")
            return

        if path == "/api/session":
            self._send_json(_principal_summary(self._headers()))
            return

        if path == "/api/storage":
            self._send_json(_storage_preview(self._headers(), storage_flow))
            return

        if path == "/api/report":
            auth = _principal_summary(self._headers())
            storage = _storage_preview(self._headers(), storage_flow)
            self._send_json(_build_report(auth, storage))
            return

        if path == "/api/recommendations":
            self._send_json(
                {
                    "checks": [
                        "Verify the sign-in provider shown by the app matches the configured IdP.",
                        "If group claims are expected, confirm your IdP actually emits them into the principal.",
                        "Configure STORAGE_BLOB_URL to test delegated storage reads.",
                        "Sign out and confirm the next browser visit requires authentication again.",
                    ]
                }
            )
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(_cfg("SAMPLE_APP_PORT", "8081") or "8081")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"{APP_TITLE} listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
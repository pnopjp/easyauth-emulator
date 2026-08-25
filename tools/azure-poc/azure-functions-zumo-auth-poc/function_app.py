"""
Minimal Azure Functions (Python v2 model) app used to verify whether Easy Auth's
header injection and X-ZUMO-AUTH support behave the same on Azure Functions as on
Azure App Service (see ../azure-zumo-auth-poc). Reuses src/_sample_app_shared.py's
principal_summary() as-is — that file is copied into this folder's deploy package
at build time (see README.md), not duplicated here, so there's one source of truth
for what "the same output shape as sample_app.py's /api/session" means.

http_auth_level=ANONYMOUS deliberately removes the Functions-native function-key
gate entirely, so this only ever tests Easy Auth's own behavior, not the two
mechanisms interacting.

C4 (see ../azure-crossorigin-zumo-poc/README.md): Easy Auth's own principal for the
client-directed (X-ZUMO-AUTH) flow only ever carries a fixed, minimal claim set
(a synthesized stable_sid, not email/preferred_username/upn) — confirmed against
real Azure even when those claims are present in the underlying access_token. To get
a reliable, human-identifiable principal on this side, this handler additionally
decodes the raw token forwarded via the provider-specific X-MS-TOKEN-<PROVIDER>-*
header itself (bypassing Easy Auth's own limited principal) and reports whichever
identity claims it carries.

The provider (from X-MS-CLIENT-PRINCIPAL-IDP) determines which header prefix to
read — real Azure names these per-provider (e.g. X-MS-TOKEN-AAD-ACCESS-TOKEN vs.
X-MS-TOKEN-GOOGLE-ID-TOKEN), unlike this repo's own Emulator, which always emits
AAD-named headers regardless of the signed-in provider. Access-token first, then
ID token as a fallback, since some providers' access_token isn't even a JWT
(e.g. Google's is opaque — its id_token is what decodes). Only aad and google have
actually been confirmed against real Azure so far; the other prefixes below are
best-effort from Microsoft's docs, not yet independently verified.
"""

import json
import sys
from pathlib import Path

import azure.functions as func

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sample_app_shared as shared  # noqa: E402

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Real Azure's per-provider token header prefix (X-MS-TOKEN-<prefix>-ACCESS-TOKEN /
# -ID-TOKEN). Only "AAD" and "GOOGLE" are confirmed against real Azure so far
# (../azure-crossorigin-zumo-poc's C4/C5 and this PoC's own Google follow-up) — the
# rest are best-effort from Microsoft's docs.
_PROVIDER_TOKEN_HEADER_PREFIX = {
    "aad": "AAD",
    "google": "GOOGLE",
    "facebook": "FACEBOOK",
    "github": "GITHUB",
    "twitter": "TWITTER",
    "microsoftaccount": "MICROSOFTACCOUNT",
    "apple": "APPLE",
}


def _provider_token_claims(headers: dict, provider: str) -> dict:
    prefix = _PROVIDER_TOKEN_HEADER_PREFIX.get(provider.lower(), provider.upper())
    for kind in ("ACCESS-TOKEN", "ID-TOKEN"):
        raw = shared._header(headers, f"X-MS-TOKEN-{prefix}-{kind}")
        claims = shared._decode_jwt_payload(raw) if raw else None
        if claims:
            return claims
    return {}


@app.route(route="session", methods=["GET"])
def session(req: func.HttpRequest) -> func.HttpResponse:
    headers = dict(req.headers)
    summary = shared.principal_summary(headers)

    provider = shared._header(headers, "X-MS-CLIENT-PRINCIPAL-IDP") or ""
    token_claims = _provider_token_claims(headers, provider)
    summary["access_token_identity"] = {
        "provider": provider,
        "oid": token_claims.get("oid", ""),
        "sub": token_claims.get("sub", ""),
        "email": token_claims.get("email", ""),
        "name": token_claims.get("name", ""),
        "preferred_username": token_claims.get("preferred_username", ""),
        "upn": token_claims.get("upn", ""),
    }

    return func.HttpResponse(
        json.dumps(summary, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        status_code=200,
    )

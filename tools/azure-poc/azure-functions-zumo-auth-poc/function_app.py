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
decodes the raw access_token forwarded via X-MS-TOKEN-AAD-ACCESS-TOKEN itself
(bypassing Easy Auth's own limited principal) and reports oid/email/preferred_username
directly.
"""

import json
import sys
from pathlib import Path

import azure.functions as func

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sample_app_shared as shared  # noqa: E402

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="session", methods=["GET"])
def session(req: func.HttpRequest) -> func.HttpResponse:
    headers = dict(req.headers)
    summary = shared.principal_summary(headers)

    access_token = shared._header(headers, "X-MS-TOKEN-AAD-ACCESS-TOKEN")
    access_token_claims = shared._decode_jwt_payload(access_token) or {}
    summary["access_token_identity"] = {
        "oid": access_token_claims.get("oid", ""),
        "email": access_token_claims.get("email", ""),
        "preferred_username": access_token_claims.get("preferred_username", ""),
        "upn": access_token_claims.get("upn", ""),
    }

    return func.HttpResponse(
        json.dumps(summary, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        status_code=200,
    )

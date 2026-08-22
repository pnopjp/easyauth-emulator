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
    summary = shared.principal_summary(dict(req.headers))
    return func.HttpResponse(
        json.dumps(summary, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        status_code=200,
    )

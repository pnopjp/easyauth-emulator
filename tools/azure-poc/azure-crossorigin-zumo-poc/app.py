"""
Minimal backend for the App Service -> Azure Functions cross-origin demo (see
README.md). Serves one HTML page whose JavaScript offers two ways to call a
*different* Azure Functions app's protected /api/session, both starting from the
same access_token already sitting in this App Service's own token store (read via
same-origin /.auth/me, cookie sent automatically — the App Service session cookie
itself never crosses origins, so it can't be used directly against Functions):

  - "Run (X-ZUMO-AUTH)": the client-directed sign-in flow — POST the access_token
    to the Functions app's /.auth/login/aad to get back its own authenticationToken,
    then call /api/session with X-ZUMO-AUTH: <authenticationToken>.
  - "Run (Authorization: Bearer)": presents the access_token directly to
    /api/session via a plain Authorization: Bearer header, skipping the
    /.auth/login/aad round trip entirely — the "daemon client application"
    (service-to-service) pattern from Microsoft's own docs, distinct from
    client-directed sign-in.

Deploy this behind the SAME App Service already used in ../azure-zumo-auth-poc
(Easy Auth/Entra ID already configured there) — see README.md.
"""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cross-origin Easy Auth demo</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
  input { width: 100%; padding: .4rem; margin: .5rem 0; box-sizing: border-box; }
  button { padding: .5rem 1rem; cursor: pointer; }
  pre { background: #f5f5f5; padding: 1rem; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
<h1>App Service &rarr; Azure Functions cross-origin demo</h1>
<p>
  This page is served by an App Service protected by Easy Auth. Its JavaScript below
  reuses the access_token already sitting in <em>this</em> App Service's token store
  (via <code>/.auth/me</code>, same-origin, cookie sent automatically) to call a
  <strong>different</strong> Azure Functions app's protected <code>/api/session</code>
  — either via the client-directed sign-in flow (<code>X-ZUMO-AUTH</code>) or by
  presenting the access_token directly as <code>Authorization: Bearer</code>.
</p>
<label>Functions app base URL
  <input id="funcBase" placeholder="https://&lt;func-app&gt;.azurewebsites.net">
</label>
<button id="runZumo">Run (X-ZUMO-AUTH)</button>
<button id="runBearer">Run (Authorization: Bearer)</button>
<pre id="out"></pre>
<script>
const out = document.getElementById('out');
function log(x) {
  out.textContent += (typeof x === 'string' ? x : JSON.stringify(x, null, 2)) + "\\n\\n";
}

function decodeJwtClaims(jwt) {
  try {
    const payload = jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(escape(atob(payload))));
  } catch (e) {
    return null;
  }
}

// Shared by both buttons: read this App Service's own stored access_token via
// same-origin /.auth/me. Returns null (after logging why) if unavailable.
async function getAccessToken() {
  log('1. GET /.auth/me (same-origin, cookie sent automatically)...');
  const meResp = await fetch('/.auth/me', { credentials: 'same-origin' });
  const me = await meResp.json();
  if (!me.length) {
    log('Not authenticated on this App Service. Sign in first, then reload this page.');
    return null;
  }
  const accessToken = me[0].access_token;
  log('Got access_token from the token store (length ' + accessToken.length + ').');
  const claims = decodeJwtClaims(accessToken);
  if (claims) {
    log('access_token claims (subset only): ' + JSON.stringify({
      aud: claims.aud, appid: claims.appid, azp: claims.azp, scp: claims.scp,
      iss: claims.iss, ver: claims.ver, tid: claims.tid,
      email: claims.email, preferred_username: claims.preferred_username, upn: claims.upn,
      iat: claims.iat, exp: claims.exp,
    }));
  }
  return accessToken;
}

function getFuncBase() {
  const funcBase = document.getElementById('funcBase').value.replace(/\\/$/, '');
  if (!funcBase) log('Enter the Functions app base URL first.');
  return funcBase || null;
}

document.getElementById('runZumo').addEventListener('click', async () => {
  out.textContent = '';
  const funcBase = getFuncBase();
  if (!funcBase) return;

  try {
    const accessToken = await getAccessToken();
    if (!accessToken) return;

    log('2. POST ' + funcBase + '/.auth/login/aad (cross-origin, client-directed sign-in)...');
    const loginResp = await fetch(funcBase + '/.auth/login/aad', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ access_token: accessToken }),
    });
    log('HTTP ' + loginResp.status);
    if (!loginResp.ok) { log('Client-directed login failed.'); return; }
    const loginJson = await loginResp.json();
    const zumoToken = loginJson.authenticationToken;
    log('Got authenticationToken (length ' + zumoToken.length + ').');

    log('3. GET ' + funcBase + '/api/session with X-ZUMO-AUTH...');
    const apiResp = await fetch(funcBase + '/api/session', {
      headers: { 'X-ZUMO-AUTH': zumoToken },
    });
    log('HTTP ' + apiResp.status);
    const apiJson = await apiResp.json();
    log(apiJson);
  } catch (err) {
    log('Error (often a CORS failure — check the browser devtools Network/Console tabs): ' + err);
  }
});

document.getElementById('runBearer').addEventListener('click', async () => {
  out.textContent = '';
  const funcBase = getFuncBase();
  if (!funcBase) return;

  try {
    const accessToken = await getAccessToken();
    if (!accessToken) return;

    // No /.auth/login/aad round trip — present the access_token straight to the
    // protected route via the standard OAuth 2.0 Authorization header, per the
    // "daemon client application" (service-to-service) pattern in Microsoft's docs.
    log('2. GET ' + funcBase + '/api/session with Authorization: Bearer...');
    const apiResp = await fetch(funcBase + '/api/session', {
      headers: { 'Authorization': 'Bearer ' + accessToken },
    });
    log('HTTP ' + apiResp.status);
    const apiJson = await apiResp.json();
    log(apiJson);
  } catch (err) {
    log('Error (often a CORS failure — check the browser devtools Network/Console tabs): ' + err);
  }
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html(_HTML)
            return
        if self.path == "/healthz":
            self._send_text("ok")
            return
        self._send_empty(404)

    def _send_html(self, markup, status=200):
        body = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"crossorigin-zumo-poc backend listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

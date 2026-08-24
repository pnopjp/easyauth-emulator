"""
Minimal backend for the App Service -> Azure Functions cross-origin demo (see
README.md). Serves one HTML page whose JavaScript:

  1. GETs /.auth/me on this same origin (the session cookie from this App Service's
     own Easy Auth login is sent automatically) to read the access_token Easy Auth
     already put in the token store.
  2. POSTs that access_token to a *different* Azure Functions app's
     /.auth/login/aad (client-directed sign-in) to get back that app's own
     authenticationToken — this step is what stands in for "a JS client that
     already has a token" per the official client-directed-flow docs.
  3. GETs the Functions app's /api/session with X-ZUMO-AUTH: <authenticationToken>
     instead of a cookie, since the App Service session cookie itself never crosses
     origins.

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
  (via <code>/.auth/me</code>, same-origin, cookie sent automatically) to sign in to a
  <strong>different</strong> Azure Functions app via its client-directed sign-in flow,
  then calls that Functions app's <code>/api/session</code> with
  <code>X-ZUMO-AUTH</code> instead of a cookie.
</p>
<label>Functions app base URL
  <input id="funcBase" placeholder="https://&lt;func-app&gt;.azurewebsites.net">
</label>
<button id="run">Run</button>
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

document.getElementById('run').addEventListener('click', async () => {
  out.textContent = '';
  const funcBase = document.getElementById('funcBase').value.replace(/\\/$/, '');
  if (!funcBase) { log('Enter the Functions app base URL first.'); return; }

  try {
    log('1. GET /.auth/me (same-origin, cookie sent automatically)...');
    const meResp = await fetch('/.auth/me', { credentials: 'same-origin' });
    const me = await meResp.json();
    if (!me.length) { log('Not authenticated on this App Service. Sign in first, then reload this page.'); return; }
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

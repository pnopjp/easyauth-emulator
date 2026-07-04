import * as vscode from 'vscode';

export interface ForwardingCheck {
    /** True when the gateway port forwards to the same local port on the client. */
    matches: boolean;
    /** Local port that actually forwards to the gateway (null if undeterminable). */
    localPort: number | null;
}

export interface SiteOrigin {
    /** 'http' or 'https'. */
    scheme: string;
    /** Hostname from site.url, e.g. 'localhost', 'test.localhost', '127.0.0.1', '[::1]'. */
    host: string;
}

/** Scheme and host of the gateway as configured in easyauth.site.url (SITE_URL). */
export function getSiteOrigin(): SiteOrigin {
    const raw = vscode.workspace.getConfiguration('easyauth').get<string>('site.url', '').trim()
        || 'http://localhost';
    const uri = vscode.Uri.parse(raw);
    const scheme = uri.scheme === 'https' ? 'https' : 'http';
    // SITE_URL carries no port (the port comes from site.port) but strip one
    // defensively; IPv6 brackets are preserved ('[::1]' has no trailing digits).
    const host = uri.authority.replace(/:(\d+)$/, '') || 'localhost';
    return { scheme, host };
}

/** True for hosts that resolve to the loopback interface on the client. */
export function isLoopbackHost(host: string): boolean {
    const h = host.toLowerCase().replace(/^\[|\]$/g, '');
    if (h === 'localhost' || h.endsWith('.localhost')) return true;
    if (h === '::1') return true;
    return /^127(\.\d{1,3}){3}$/.test(h);
}

/**
 * The OAuth callback URL is built by the emulator from SITE_URL/SITE_PORT.
 * The forwarding check is only meaningful in remote sessions where the
 * gateway is addressed via a loopback host — the browser then reaches it
 * through VS Code port forwarding on the client. With a non-loopback
 * site.url the user manages routing themselves.
 */
export function forwardingCheckApplies(): boolean {
    if (vscode.env.remoteName === undefined) return false;
    return isLoopbackHost(getSiteOrigin().host);
}

/**
 * Verify that the gateway port forwards to the same local port on the client
 * — new OAuth logins only work then (the IdP redirects the browser to
 * <site.url>:<site.port>).
 *
 * asExternalUri reuses an already-registered forward for the port, so a
 * mismatch has two possible causes the API cannot distinguish:
 * - a stale Ports-panel entry mapping the gateway port to another local port
 *   (left over from an earlier attempt when the local port really was busy),
 * - the local port is genuinely unavailable on the client (in use — or on
 *   Windows reserved by Hyper-V/WSL even when netstat shows it free),
 * so error messages must offer both remedies. The probe uses http://localhost
 * because that form is guaranteed to be recognized by asExternalUri; the
 * tunnel is plain TCP, so the gateway's actual scheme and loopback hostname
 * do not matter. The forwarding it establishes is intentionally left open
 * for the browser.
 */
export async function checkPortForwarding(sitePort: number): Promise<ForwardingCheck> {
    const externalUri = await vscode.env.asExternalUri(
        vscode.Uri.parse(`http://localhost:${sitePort}`)
    );
    const m = /:(\d+)$/.exec(externalUri.authority);
    const localPort = m ? Number(m[1]) : null;
    return { matches: localPort === sitePort, localPort };
}

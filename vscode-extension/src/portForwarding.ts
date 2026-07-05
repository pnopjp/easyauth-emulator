import * as vscode from 'vscode';

export type ForwardingCheck =
    /** localhost:<site.port> on the client reaches the gateway — all good. */
    | { kind: 'match'; localPort: number }
    /** The gateway port forwards to a DIFFERENT local port — new OAuth logins break. */
    | { kind: 'mismatch'; localPort: number }
    /**
     * The client exposes the gateway through a forwarded domain URL instead of
     * localhost (Remote - Tunnels / Codespaces web style, e.g.
     * https://xxx-8080.devtunnels.ms). The site is reachable there, but OAuth
     * callbacks (SITE_URL = localhost) do not go through that domain.
     */
    | { kind: 'external-domain'; externalUri: vscode.Uri }
    /** The returned URI could not be interpreted — do not act on it. */
    | { kind: 'unknown'; externalUri: vscode.Uri };

export interface SiteOrigin {
    /** 'http' or 'https'. */
    scheme: string;
    /** Hostname from site.url, e.g. 'localhost', 'test.localhost', '127.0.0.1', '[::1]'. */
    host: string;
    /** Port explicitly included in site.url, or null (IPv6 brackets don't count). */
    explicitPort: number | null;
}

/** Scheme, host and optional explicit port of the gateway as configured in easyauth.site.url (SITE_URL). */
export function getSiteOrigin(): SiteOrigin {
    const raw = vscode.workspace.getConfiguration('easyauth').get<string>('site.url', '').trim()
        || 'http://localhost';
    const uri = vscode.Uri.parse(raw);
    const scheme = uri.scheme === 'https' ? 'https' : 'http';
    const m = /:(\d+)$/.exec(uri.authority);
    const explicitPort = m ? Number(m[1]) : null;
    const host = uri.authority.replace(/:(\d+)$/, '') || 'localhost';
    return { scheme, host, explicitPort };
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
 * Resolve how the client reaches the gateway port and classify the result —
 * new OAuth logins only work when it is localhost on the same port (the IdP
 * redirects the browser to <site.url>:<site.port>).
 *
 * asExternalUri reuses an already-registered forward for the port, so a
 * 'mismatch' has two possible causes the API cannot distinguish:
 * - a stale Ports-panel entry mapping the gateway port to another local port
 *   (left over from an earlier attempt when the local port really was busy),
 * - the local port is genuinely unavailable on the client (in use — or on
 *   Windows reserved by Hyper-V/WSL even when netstat shows it free),
 * so error messages must offer both remedies. The probe uses http://localhost
 * because that form is guaranteed to be recognized by asExternalUri; the
 * tunnel is plain TCP, so the gateway's actual scheme and loopback hostname
 * do not matter. A forwarding it establishes is intentionally left open for
 * the browser.
 */
export async function checkPortForwarding(sitePort: number): Promise<ForwardingCheck> {
    const externalUri = await vscode.env.asExternalUri(
        vscode.Uri.parse(`http://localhost:${sitePort}`)
    );
    const authority = externalUri.authority;
    if (!authority) {
        return { kind: 'unknown', externalUri };
    }
    const host = authority.replace(/:(\d+)$/, '');
    if (!isLoopbackHost(host)) {
        // e.g. Remote - Tunnels returning https://xxx-8080.devtunnels.ms/
        return { kind: 'external-domain', externalUri };
    }
    const m = /:(\d+)$/.exec(authority);
    const localPort = m ? Number(m[1]) : (externalUri.scheme === 'https' ? 443 : 80);
    return localPort === sitePort
        ? { kind: 'match', localPort }
        : { kind: 'mismatch', localPort };
}

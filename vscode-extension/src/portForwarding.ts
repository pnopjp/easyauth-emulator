import * as vscode from 'vscode';

export interface ForwardingCheck {
    /** Client-side URI returned by asExternalUri (host may differ from site.url). */
    externalUri: vscode.Uri;
    /** Local port the client actually forwards to the remote gateway. */
    externalPort: number;
    /** True when the forwarded local port equals site.port. */
    matches: boolean;
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
 * Establish port forwarding for the gateway and report whether VS Code
 * forwarded it on the same local port. In a remote session VS Code picks a
 * different local port when site.port is already taken on the client — the
 * IdP then redirects new logins to <site.url>:<site.port>, which no longer
 * reaches the gateway. The probe always uses http://localhost because that
 * form is guaranteed to be recognized by asExternalUri; the tunnel is plain
 * TCP, so the gateway's actual scheme and loopback hostname do not matter.
 */
export async function checkPortForwarding(sitePort: number): Promise<ForwardingCheck> {
    const externalUri = await vscode.env.asExternalUri(
        vscode.Uri.parse(`http://localhost:${sitePort}`)
    );
    const m = /:(\d+)$/.exec(externalUri.authority);
    const externalPort = m ? Number(m[1]) : (externalUri.scheme === 'https' ? 443 : 80);
    return { externalUri, externalPort, matches: externalPort === sitePort };
}

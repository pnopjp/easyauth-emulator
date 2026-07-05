import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { StatusBarManager, EmulatorState } from './statusBar';
import { SecretManager } from './secretManager';
import { checkPortForwarding, forwardingCheckApplies, getSiteOrigin } from './portForwarding';

const STARTUP_TIMEOUT_MS = 30_000;
const STARTED_MARKER = 'All processes started';

export class EmulatorManager implements vscode.Disposable {
    private state: EmulatorState = 'stopped';
    private process: cp.ChildProcess | null = null;
    private sessionId: string | null = null;
    private startTimeout: ReturnType<typeof setTimeout> | null = null;
    private currentPort: number | null = null;

    private readonly _onDidChangeState = new vscode.EventEmitter<EmulatorState>();
    readonly onDidChangeState: vscode.Event<EmulatorState> = this._onDidChangeState.event;

    constructor(
        private readonly context: vscode.ExtensionContext,
        private readonly outputChannel: vscode.LogOutputChannel,
        private readonly statusBar: StatusBarManager,
        private readonly secretManager: SecretManager,
    ) {
        this.setState(this.hasConfig() ? 'stopped' : 'unconfigured');
        void this.updateStateForSecrets();
    }

    isManaging(): boolean {
        return this.sessionId !== null;
    }

    isManagingSession(sessionId: string): boolean {
        return this.sessionId === sessionId;
    }

    getState(): EmulatorState {
        return this.state;
    }

    getCurrentPort(): number | null {
        return this.currentPort;
    }

    async start(port: number, sessionId: string): Promise<void> {
        if (this.state !== 'stopped' && this.state !== 'missing_secret' && this.state !== 'missing_entra_issuer') {
            if (this.currentPort !== port) {
                // Port changed — restart with new port
                await this.stop();
            } else {
                return;
            }
        }

        const resolved = this.resolveBinary();
        if (!resolved) {
            vscode.window.showErrorMessage(
                'EasyAuth Emulator binary not found.'
            );
            return;
        }

        if (!this.hasConfig()) {
            this.outputChannel.warn('[extension] Config file not found. Create .vscode/easyauth.json in the workspace root, or configure IDP settings.');
            this.setState('unconfigured');
            return;
        }

        const secretsOk = await this.secretManager.promptForMissingSecrets(this.outputChannel);
        if (!secretsOk) return;

        const { cmd, cmdArgs } = this.buildArgs(resolved);
        this.sessionId = sessionId;
        this.currentPort = port;
        this.setState('starting');

        this.outputChannel.info(`[extension] Starting EasyAuth Emulator on upstream port ${port}`);
        this.outputChannel.info(`[extension] $ ${cmd} ${cmdArgs.join(' ')}`);

        try {
            const env = await this.buildEnv(port);
            const proc = cp.spawn(cmd, cmdArgs, {
                cwd: this.workspaceRoot(),
                env,
                stdio: ['ignore', 'pipe', 'pipe'],
                windowsHide: true,
            });
            this.process = proc;

            let stdoutBuf = '';
            proc.stdout?.on('data', (data: Buffer) => {
                stdoutBuf += data.toString();
                const lines = stdoutBuf.split(/\r?\n/);
                stdoutBuf = lines.pop() ?? '';
                for (const line of lines) {
                    if (/\] Error /.test(line)) {
                        this.outputChannelError(line);
                    } else if (/\] (Warning|WARNING): /.test(line)) {
                        this.outputChannel.warn(line);
                    } else {
                        this.outputChannel.appendLine(line);
                    }
                    if (this.state === 'starting' && line.includes(STARTED_MARKER)) {
                        this.clearStartTimeout();
                        this.setState('running');
                        void this.verifyPortForwarding();
                    }
                }
            });

            let stderrBuf = '';
            proc.stderr?.on('data', (data: Buffer) => {
                stderrBuf += data.toString();
                const lines = stderrBuf.split(/\r?\n/);
                stderrBuf = lines.pop() ?? '';
                for (const line of lines) {
                    this.outputChannelError(line);
                }
            });

            proc.on('exit', (code) => {
                // Ignore stale exit events from a process that was already replaced
                if (this.process !== proc) { return; }
                this.clearStartTimeout();
                if (this.state !== 'stopped') {
                    if (code !== 0 && code !== null) {
                        this.setState('error');
                        this.notifyError();
                    } else {
                        this.setState('stopped');
                    }
                }
                this.process = null;
                this.sessionId = null;
                this.currentPort = null;
                if (this.state === 'stopped') {
                    void this.updateStateForSecrets();
                }
            });

            proc.on('error', (err) => {
                // Ignore stale error events from a process that was already replaced
                if (this.process !== proc) { return; }
                this.clearStartTimeout();
                this.outputChannelError(`[extension] Spawn error: ${err.message}`);
                this.setState('error');
                this.process = null;
                this.sessionId = null;
                this.currentPort = null;
            });

            this.startTimeout = setTimeout(() => {
                if (this.state === 'starting') {
                    this.outputChannelError(
                        `[extension] Startup timeout: "${STARTED_MARKER}" not detected within 30 s`
                    );
                    this.setState('error');
                    this.notifyError();
                }
            }, STARTUP_TIMEOUT_MS);
        } catch (err) {
            this.outputChannelError(`[extension] Error: ${err}`);
            this.setState('error');
            this.sessionId = null;
            this.currentPort = null;
        }
    }

    async stop(): Promise<void> {
        this.clearStartTimeout();
        if (this.process) {
            this.outputChannel.info('[extension] Stopping EasyAuth Emulator');
            const pid = this.process.pid;
            this.process = null;
            if (process.platform === 'win32' && pid !== undefined) {
                // taskkill /T kills the entire process tree (start.py + oauth2-proxy children)
                cp.spawnSync('taskkill', ['/F', '/T', '/PID', String(pid)], { windowsHide: true });
            } else {
                try { cp.spawnSync('kill', ['-TERM', String(pid)]); } catch { /* ignore */ }
            }
        }
        this.sessionId = null;
        this.currentPort = null;
        this.setState('stopped');
    }

    async restart(): Promise<void> {
        const port = this.currentPort;
        const sessionId = this.sessionId;
        await this.stop();
        if (port !== null && sessionId !== null) {
            await this.start(port, sessionId);
        }
    }

    dispose(): void {
        void this.stop();
        this._onDidChangeState.dispose();
    }

    private setState(state: EmulatorState): void {
        this.state = state;
        void vscode.commands.executeCommand('setContext', 'easyauth.running', state === 'running');
        void vscode.commands.executeCommand('setContext', 'easyauth.state', state);
        const listenPort = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
        this.statusBar.update(state, listenPort, this.currentPort);
        this._onDidChangeState.fire(state);
    }

    /**
     * In a remote session, new OAuth logins only work when VS Code forwards
     * site.port to the same local port on the client (the IdP redirects the
     * browser to http://localhost:<site.port>). Only the confirmed-broken case
     * (a loopback forward on a different port) stops the emulator; forwarded
     * domain URLs (Remote - Tunnels / Codespaces web) and unrecognized results
     * are logged as warnings instead so unknown environments are not killed
     * by a false positive.
     */
    private async verifyPortForwarding(): Promise<void> {
        if (!forwardingCheckApplies()) return;
        const sitePort = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
        let check;
        try {
            check = await checkPortForwarding(sitePort);
        } catch (err) {
            this.outputChannel.warn(`[extension] Port forwarding check failed: ${err}`);
            return;
        }
        // State may have changed while awaiting (e.g. stopped by the user)
        if (this.state !== 'running') return;

        switch (check.kind) {
            case 'match':
                this.outputChannel.info(
                    `[extension] Port forwarding OK: http://localhost:${sitePort} on your PC reaches the emulator.`
                );
                return;
            case 'external-domain':
                this.outputChannel.warn(
                    `[extension] This environment exposes the emulator through a forwarded URL: ${check.externalUri.toString(true)}\n` +
                    `The site is reachable there, but OAuth sign-in through that URL is not supported yet ` +
                    `(login callbacks go to http://localhost:${sitePort}).\n` +
                    `To sign in: open the PORTS panel (bottom panel, next to TERMINAL), forward port ${sitePort}, ` +
                    `and open http://localhost:${sitePort} on your PC.`
                );
                return;
            case 'unknown':
                this.outputChannel.warn(
                    `[extension] Could not interpret the forwarded address for port ${sitePort}: ` +
                    `${check.externalUri.toString(true)} — skipping the port forwarding check.`
                );
                return;
            case 'mismatch':
                break;
        }

        const { scheme, host } = getSiteOrigin();
        this.outputChannelError(
            `[extension] Error: port ${sitePort} is forwarded to a DIFFERENT local port (${check.localPort}) on your PC.\n` +
            `OAuth login callbacks go to ${scheme}://${host}:${sitePort} on your PC and would not reach the emulator, so it has been stopped.\n` +
            `To fix:\n` +
            `  1. Open the PORTS panel (bottom panel, next to TERMINAL), right-click the entry for port ${sitePort} and select "Stop Forwarding Port".\n` +
            `  2. Quit the app on your PC that is using port ${sitePort} — or change the "easyauth.site.port" setting to a free port.\n` +
            `     (On Windows the port may also be reserved by Hyper-V/WSL even when it looks free — check with: netsh interface ipv4 show excludedportrange protocol=tcp)\n` +
            `  3. Start the emulator again.`
        );
        await this.stop();
        this.setState('error');
        void vscode.window.showErrorMessage(
            `EasyAuth: emulator stopped — port ${sitePort} is forwarded to a different local port (${check.localPort}), ` +
            `so OAuth login would fail. Fix: 1) In the PORTS panel, stop forwarding port ${sitePort}. ` +
            `2) Quit the app using port ${sitePort} on your PC, or change the easyauth.site.port setting. ` +
            `3) Start the emulator again.`,
            'Open Output'
        ).then((sel) => {
            if (sel === 'Open Output') {
                this.outputChannel.show();
            }
        });
    }

    private clearStartTimeout(): void {
        if (this.startTimeout !== null) {
            clearTimeout(this.startTimeout);
            this.startTimeout = null;
        }
    }

    private notifyError(): void {
        vscode.window.showErrorMessage(
            'EasyAuth Emulator exited unexpectedly.',
            'Open Output'
        ).then((sel) => {
            if (sel === 'Open Output') {
                this.outputChannel.show();
            }
        });
    }

    private resolveBinary(): string | null {
        // Bundled binary (packed into the VSIX under /bin/)
        const extDir = this.context.extensionPath;
        const exe = process.platform === 'win32' ? 'easyauth-emulator.exe' : 'easyauth-emulator';
        const bundled = path.join(extDir, 'bin', 'easyauth-emulator', exe);
        if (fs.existsSync(bundled)) return bundled;

        // Development fallback: start.py lives one level above the extension directory
        const devStartPy = path.resolve(extDir, '..', 'start.py');
        if (fs.existsSync(devStartPy)) return devStartPy;

        return null;
    }

    private buildArgs(binaryPath: string): { cmd: string; cmdArgs: string[] } {
        const extraArgs: string[] = [];

        // Always pass --config pointing to .vscode/easyauth.toml.
        // If the file exists it is loaded as a base config; if not, auto-discovery is suppressed
        // (prevents accidentally reading a project-owned config.toml in the workspace root).
        const wsRoot = this.workspaceRoot();
        if (wsRoot) {
            const configPath = path.join(wsRoot, '.vscode', 'easyauth.toml');
            extraArgs.push('--config', configPath);
            if (fs.existsSync(configPath)) {
                this.outputChannel.info(`[extension] Using config: ${configPath}`);
            }
        }

        if (vscode.workspace.getConfiguration('easyauth').get<boolean>('verbose', false)) {
            extraArgs.push('--verbose');
        }

        if (binaryPath.endsWith('.py')) {
            return { cmd: 'python', cmdArgs: ['-u', binaryPath, ...extraArgs] };
        }
        return { cmd: binaryPath, cmdArgs: extraArgs };
    }

    hasConfig(): boolean {
        const vsConfig = vscode.workspace.getConfiguration('easyauth');
        const builtins = ['entra', 'google', 'facebook', 'apple', 'github'];
        if (builtins.some(idp => vsConfig.get<string>(`${idp}.clientId`, '').trim())) return true;
        const customs = vsConfig.get<Array<{ name?: string; clientId?: string }>>('customIdps', []);
        return customs.some(idp => idp.name?.trim() && idp.clientId?.trim());
    }

    onConfigurationChanged(): void {
        if (this.state === 'starting' || this.state === 'running' || this.state === 'error') {
            return;
        }
        if (!this.hasConfig()) {
            this.setState('unconfigured');
            return;
        }
        void this.updateStateForSecrets();
    }

    async updateStateForSecrets(): Promise<void> {
        if (this.isActiveState()) {
            return;
        }
        if (!this.hasConfig()) {
            this.setState('unconfigured');
            return;
        }
        const hasUnset = await this.secretManager.hasUnsetSecrets();
        // Re-check after async: state may have changed while awaiting (e.g. emulator restarted)
        if (this.isActiveState()) {
            return;
        }
        if (hasUnset) {
            this.setState('missing_secret');
            return;
        }
        if (this.hasEntraMissingIssuerUrl()) {
            this.setState('missing_entra_issuer');
            return;
        }
        this.setState('stopped');
    }

    private isActiveState(): boolean {
        return this.state === 'starting' || this.state === 'running' || this.state === 'error';
    }

    private hasEntraMissingIssuerUrl(): boolean {
        const config = vscode.workspace.getConfiguration('easyauth');
        if (!config.get<string>('entra.clientId', '').trim()) return false;
        return !config.get<string>('entra.oidcIssuerUrl', '').trim();
    }

    private async buildEnv(port: number): Promise<NodeJS.ProcessEnv> {
        const config = vscode.workspace.getConfiguration('easyauth');
        const extra: Record<string, string> = {};

        extra['PYTHONUNBUFFERED'] = '1';
        extra['APP_UPSTREAM'] = `http://localhost:${port}`;

        // Site
        const siteUrl = config.get<string>('site.url', '').trim();
        if (siteUrl) extra['SITE_URL'] = siteUrl;
        const sitePort = config.get<number | null>('site.port', null);
        if (sitePort !== null) extra['SITE_PORT'] = String(sitePort);
        const tlsCertFile = config.get<string>('tls.certFile', '').trim();
        if (tlsCertFile) extra['TLS_CERT_FILE'] = tlsCertFile;
        const tlsKeyFile = config.get<string>('tls.keyFile', '').trim();
        if (tlsKeyFile) extra['TLS_KEY_FILE'] = tlsKeyFile;

        // Global IDP settings
        const defaultIdp = config.get<string>('defaultIdp', '').trim();
        if (defaultIdp) extra['DEFAULT_IDP'] = defaultIdp;
        const skipAuthRoutes = config.get<string>('skipAuthRoutes', '').trim();
        if (skipAuthRoutes) extra['SKIP_AUTH_ROUTES'] = skipAuthRoutes;
        if (config.get<boolean>('debugHeadersEndpointEnabled', false)) {
            extra['DEBUG_HEADERS_ENDPOINT_ENABLED'] = 'true';
        }
        const idpSelectIcons = config.get<string>('idpSelectIcons', '').trim();
        if (idpSelectIcons) extra['IDP_SELECT_ICONS'] = idpSelectIcons;

        // Built-in IDPs
        const BUILTIN_IDPS: Array<[string, string]> = [
            ['entra', 'ENTRA'],
            ['google', 'GOOGLE'],
            ['facebook', 'FACEBOOK'],
            ['apple', 'APPLE'],
            ['github', 'GITHUB'],
        ];
        const idpList: string[] = [];
        for (const [idpName, envKey] of BUILTIN_IDPS) {
            const clientId = config.get<string>(`${idpName}.clientId`, '').trim();
            if (!clientId) continue;
            const clientSecret = await this.secretManager.get(idpName);
            if (!clientSecret) {
                this.outputChannel.warn(`[extension] Warning: ${idpName} clientId is set but no client secret found — run "EasyAuth Emulator: Set Client Secret"`);
                continue;
            }
            idpList.push(idpName);
            extra[`IDP_${envKey}_CLIENT_ID`] = clientId;
            extra[`IDP_${envKey}_CLIENT_SECRET`] = clientSecret;
            const displayName = config.get<string>(`${idpName}.displayName`, '').trim();
            if (displayName) extra[`IDP_${envKey}_DISPLAY_NAME`] = displayName;
            const scopes = config.get<string>(`${idpName}.scopes`, '').trim();
            if (scopes) extra[`IDP_${envKey}_SCOPES`] = scopes;
            const authUserIdClaim = config.get<string>(`${idpName}.authUserIdClaim`, '').trim();
            if (authUserIdClaim) extra[`IDP_${envKey}_AUTH_USER_ID_CLAIM`] = authUserIdClaim;
            const extraArgs = config.get<string>(`${idpName}.extraArgs`, '').trim();
            if (extraArgs) extra[`IDP_${envKey}_EXTRA_ARGS`] = extraArgs;
            const icon = config.get<string>(`${idpName}.icon`, '').trim();
            if (icon) extra[`IDP_${envKey}_ICON`] = icon;
        }

        // Entra-specific: full OIDC issuer URL
        const entraIssuerUrl = config.get<string>('entra.oidcIssuerUrl', '').trim();
        if (entraIssuerUrl) {
            extra['IDP_ENTRA_OIDC_ISSUER_URL'] = entraIssuerUrl;
        }

        // Custom OIDC IDPs
        interface CustomIdp {
            name: string;
            clientId: string;
            oidcIssuerUrl: string;
            displayName?: string;
            scopes?: string;
            authProvider?: string;
            authUserIdClaim?: string;
            prompt?: string;
            codeChallengeMethod?: string;
            logoutEndpoint?: string;
            skipClaimsFromProfileUrl?: boolean;
            extraArgs?: string;
            icon?: string;
        }
        const customIdps = config.get<CustomIdp[]>('customIdps', []);
        for (const idp of customIdps) {
            const name = idp.name?.trim();
            if (!name || !idp.clientId?.trim()) continue;
            const clientSecret = await this.secretManager.get(`custom:${name}`);
            if (!clientSecret) {
                this.outputChannel.warn(`[extension] Warning: custom IDP '${name}' clientId is set but no client secret found — run "EasyAuth Emulator: Set Client Secret"`);
                continue;
            }
            idpList.push(name);
            const envKey = name.toUpperCase().replace(/-/g, '_');
            extra[`IDP_${envKey}_CLIENT_ID`] = idp.clientId.trim();
            extra[`IDP_${envKey}_CLIENT_SECRET`] = clientSecret;
            extra[`IDP_${envKey}_OIDC_ISSUER_URL`] = idp.oidcIssuerUrl.trim();
            if (idp.displayName?.trim()) extra[`IDP_${envKey}_DISPLAY_NAME`] = idp.displayName.trim();
            if (idp.scopes?.trim()) extra[`IDP_${envKey}_SCOPES`] = idp.scopes.trim();
            if (idp.authProvider?.trim()) extra[`IDP_${envKey}_AUTH_PROVIDER`] = idp.authProvider.trim();
            if (idp.authUserIdClaim?.trim()) extra[`IDP_${envKey}_AUTH_USER_ID_CLAIM`] = idp.authUserIdClaim.trim();
            if (idp.prompt?.trim()) extra[`IDP_${envKey}_PROMPT`] = idp.prompt.trim();
            if (idp.codeChallengeMethod?.trim()) extra[`IDP_${envKey}_CODE_CHALLENGE_METHOD`] = idp.codeChallengeMethod.trim();
            if (idp.logoutEndpoint?.trim()) extra[`IDP_${envKey}_LOGOUT_ENDPOINT`] = idp.logoutEndpoint.trim();
            if (idp.skipClaimsFromProfileUrl) extra[`IDP_${envKey}_SKIP_CLAIMS_FROM_PROFILE_URL`] = 'true';
            if (idp.extraArgs?.trim()) extra[`IDP_${envKey}_EXTRA_ARGS`] = idp.extraArgs.trim();
            if (idp.icon?.trim()) extra[`IDP_${envKey}_ICON`] = idp.icon.trim();
        }

        if (idpList.length > 0) extra['IDP_LIST'] = idpList.join(',');

        // oauth2-proxy settings
        const oauth2 = vscode.workspace.getConfiguration('easyauth.oauth2proxy');
        const portBase = oauth2.get<number | null>('portBase', null);
        if (portBase !== null) extra['OAUTH2_PROXY_PORT_BASE'] = String(portBase);
        if (oauth2.get<boolean>('showDebugOnError', false)) extra['OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR'] = 'true';
        if (oauth2.get<boolean>('standardLogging', false)) extra['OAUTH2_PROXY_STANDARD_LOGGING'] = 'true';
        if (oauth2.get<boolean>('authLogging', false)) extra['OAUTH2_PROXY_AUTH_LOGGING'] = 'true';
        if (oauth2.get<boolean>('requestLogging', false)) extra['OAUTH2_PROXY_REQUEST_LOGGING'] = 'true';
        const proxyVersion = oauth2.get<string>('version', '').trim();
        if (proxyVersion) extra['OAUTH2_PROXY_VERSION'] = proxyVersion;
        if (oauth2.get<boolean>('autoUpdate', false)) extra['OAUTH2_PROXY_AUTO_UPDATE'] = 'true';
        const sslCaBundle = oauth2.get<string>('sslCaBundle', '').trim();
        if (sslCaBundle) extra['SSL_CA_BUNDLE'] = sslCaBundle;
        const trustedProxyIp = oauth2.get<string>('trustedProxyIp', '').trim();
        if (trustedProxyIp) extra['OAUTH2_PROXY_TRUSTED_PROXY_IP'] = trustedProxyIp;

        // Cookie secret: generate once and persist per workspace in SecretStorage (encrypted)
        extra['OAUTH2_PROXY_COOKIE_SECRET'] = await this.secretManager.getCookieSecret();

        return { ...process.env, ...extra };
    }

    private outputChannelError(message: string): void {
        this.outputChannel.error(message);
        this.outputChannel.show(true);
    }

    private workspaceRoot(): string | undefined {
        return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    }
}

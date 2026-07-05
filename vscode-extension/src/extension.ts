import * as vscode from 'vscode';
import * as cp from 'child_process';
import { EmulatorManager } from './emulatorManager';
import { PortDetector } from './portDetector';
import { StatusBarManager } from './statusBar';
import { SecretManager } from './secretManager';
import { EmulatorTreeProvider, IdpInfo, IdpTreeItem } from './emulatorTreeProvider';
import { checkPortForwarding, forwardingCheckApplies, getSiteOrigin, isLoopbackHost } from './portForwarding';

// URL of the gateway as the browser should reach it. For a loopback site.url
// the gateway is addressed directly, so the listen port (site.port) applies.
// A non-loopback site.url is a full public origin the user configured (tunnel
// domain, reverse proxy, ...) — never bolt the listen port onto it; honor an
// explicit port only when site.url itself contains one.
function gatewayUrl(urlPath: string, listenPort: number): string {
    const { scheme, host, explicitPort } = getSiteOrigin();
    const port = isLoopbackHost(host) ? listenPort : explicitPort;
    const authority = port !== null ? `${host}:${port}` : host;
    return `${scheme}://${authority}${urlPath}`;
}

const BUILTIN_IDP_LABELS: Record<string, string> = {
    entra: 'Microsoft Entra ID',
    google: 'Google',
    facebook: 'Facebook',
    apple: 'Apple',
    github: 'GitHub',
};

// Default Simple Icons slugs for built-in IDPs (used when no custom icon is configured).
const BUILTIN_DEFAULT_ICONS: Record<string, string> = {
    entra: 'microsoft',
    google: 'google',
    facebook: 'facebook',
    apple: 'apple',
    github: 'github',
};

// IDP keys are used verbatim in URLs — only letters, digits, hyphens, underscores.
const SAFE_IDP_KEY = /^[A-Za-z0-9_-]+$/;

function getConfiguredIdps(): IdpInfo[] {
    const config = vscode.workspace.getConfiguration('easyauth');
    const idps: IdpInfo[] = [];
    for (const [key, label] of Object.entries(BUILTIN_IDP_LABELS)) {
        if (config.get<string>(`${key}.clientId`, '').trim()) {
            const configIcon = config.get<string>(`${key}.icon`, '').trim();
            const icon = configIcon || BUILTIN_DEFAULT_ICONS[key];
            idps.push({ key, displayName: label, icon });
        }
    }
    interface CustomIdp { name?: string; clientId?: string; displayName?: string; icon?: string; }
    const customIdps = config.get<CustomIdp[]>('customIdps', []);
    for (const idp of customIdps) {
        const name = idp.name?.trim();
        if (name && idp.clientId?.trim() && SAFE_IDP_KEY.test(name)) {
            const icon = idp.icon?.trim() || undefined;
            idps.push({ key: name, displayName: idp.displayName?.trim() || name, icon });
        }
    }
    return idps;
}

function getIconsMode(): string {
    return vscode.workspace.getConfiguration('easyauth').get<string>('idpSelectIcons', 'simple');
}

export function activate(context: vscode.ExtensionContext): void {
    if (vscode.env.uiKind === vscode.UIKind.Web) {
        void vscode.window.showErrorMessage('EasyAuth Emulator is not supported in VS Code for Web.');
        return;
    }

    const outputChannel = vscode.window.createOutputChannel('EasyAuth Emulator', { log: true });
    const statusBar = new StatusBarManager();
    const secretManager = new SecretManager(context);
    const emulator = new EmulatorManager(context, outputChannel, statusBar, secretManager);
    const portDetector = new PortDetector(outputChannel);

    context.subscriptions.push(outputChannel, statusBar, emulator);

    const treeProvider = new EmulatorTreeProvider();
    const treeView = vscode.window.createTreeView('easyauth.statusView', {
        treeDataProvider: treeProvider,
        showCollapseAll: false,
    });
    treeProvider.update(emulator.getState());
    treeProvider.updateIdps(getConfiguredIdps(), getIconsMode());
    treeView.description = emulator.getState();

    function updatePrivateBrowserContext(): void {
        const cmd = vscode.workspace.getConfiguration('easyauth').get<string>('privateBrowser.command', '').trim();
        void vscode.commands.executeCommand('setContext', 'easyauth.privateBrowserConfigured', !!cmd);
    }

    async function updateSecretContext(): Promise<void> {
        const needs = await secretManager.hasUnsetSecrets();
        void vscode.commands.executeCommand('setContext', 'easyauth.needsSecret', needs);
    }

    updatePrivateBrowserContext();
    void updateSecretContext();
    // Remote sessions swap the private-browser buttons for copy-URL buttons
    // (a browser cannot be launched on the client PC from the remote host).
    void vscode.commands.executeCommand('setContext', 'easyauth.isRemote', vscode.env.remoteName !== undefined);

    context.subscriptions.push(
        treeProvider,
        treeView,
        emulator.onDidChangeState(state => {
            treeProvider.update(state);
            treeProvider.updateIdps(getConfiguredIdps(), getIconsMode());
            treeView.description = state;
        }),
        vscode.workspace.onDidChangeConfiguration(e => {
            if (e.affectsConfiguration('easyauth')) {
                updatePrivateBrowserContext();
                void updateSecretContext();
                treeProvider.updateIdps(getConfiguredIdps(), getIconsMode());
                emulator.onConfigurationChanged();
            }
        }),
        context.secrets.onDidChange(() => {
            void updateSecretContext();
            void emulator.updateStateForSecrets();
        }),
    );

    let outputShownSinceError = false;

    // Capture debug output for Step 4 (stdout port detection)
    context.subscriptions.push(
        vscode.debug.registerDebugAdapterTrackerFactory('*', {
            createDebugAdapterTracker(_session) {
                return {
                    onDidSendMessage(msg: { type: string; event?: string; body?: { output?: string } }) {
                        if (msg.type === 'event' && msg.event === 'output' && msg.body?.output) {
                            portDetector.onDebugOutput(msg.body.output);
                        }
                    },
                };
            },
        }),
        // Shell integration output (VS Code 1.93+): covers integratedTerminal where
        // app output bypasses DAP output events.
        vscode.window.onDidStartTerminalShellExecution(e => {
            portDetector.onShellExecution(e.execution);
        })
    );

    // Debug session started
    context.subscriptions.push(
        vscode.debug.onDidStartDebugSession(async (session) => {
            const config = vscode.workspace.getConfiguration('easyauth');
            if (!config.get<boolean>('autoStart', true)) return;
            if (!emulator.hasConfig()) return;
            // Ignore child sessions (e.g. debugpy spawns one per Flask
            // reloader subprocess) — they belong to the same debug run and
            // would double-start the emulator when the parent's instance has
            // already stopped (e.g. after a failed port forwarding check).
            if (session.parentSession) return;
            // Only attach to the first session; ignore subsequent ones
            if (emulator.isManaging()) return;

            portDetector.resetForNewSession();
            const port = await portDetector.detect(session, context.workspaceState);
            if (port === null) return;

            outputShownSinceError = false;
            await emulator.start(port, session.id);
        })
    );

    // Debug session ended
    context.subscriptions.push(
        vscode.debug.onDidTerminateDebugSession(async (session) => {
            const config = vscode.workspace.getConfiguration('easyauth');
            if (!config.get<boolean>('autoStop', true)) return;
            if (!emulator.isManagingSession(session.id)) return;

            await emulator.stop();
            void emulator.updateStateForSecrets();
        })
    );

    // Commands
    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.statusBarClick', async () => {
            const state = emulator.getState();
            switch (state) {
                case 'unconfigured':
                    await vscode.commands.executeCommand('workbench.action.openWorkspaceSettings', '@ext:pnop.easyauth-emulator');
                    break;
                case 'missing_secret':
                    await secretManager.promptMissingSecretsFromStatusBar();
                    // State updates automatically via context.secrets.onDidChange
                    break;
                case 'missing_entra_issuer':
                    await vscode.commands.executeCommand('workbench.action.openWorkspaceSettings', 'easyauth.entra.oidcIssuerUrl');
                    break;
                case 'starting':
                    outputChannel.show();
                    break;
                case 'running':
                    await vscode.commands.executeCommand('easyauth.openInBrowser');
                    break;
                case 'stopped': {
                    portDetector.resetForNewSession();
                    const port = await portDetector.detectManual(context.workspaceState);
                    if (port !== null) {
                        outputShownSinceError = false;
                        const sessionId = vscode.debug.activeDebugSession?.id ?? '__manual__';
                        await emulator.start(port, sessionId);
                    }
                    break;
                }
                case 'error':
                    if (!outputShownSinceError) {
                        outputChannel.show();
                        outputShownSinceError = true;
                    } else {
                        outputShownSinceError = false;
                        portDetector.resetForNewSession();
                        const port = await portDetector.detectManual(context.workspaceState);
                        if (port !== null) {
                            const sessionId = vscode.debug.activeDebugSession?.id ?? '__manual__';
                            await emulator.start(port, sessionId);
                        }
                    }
                    break;
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.openSettings', async () => {
            await vscode.commands.executeCommand('workbench.action.openWorkspaceSettings', '@ext:pnop.easyauth-emulator');
        })
    );

    function launchInPrivateBrowser(url: string): void {
        const cmd = vscode.workspace.getConfiguration('easyauth').get<string>('privateBrowser.command', '').trim();
        if (!cmd) {
            void vscode.window.showErrorMessage(
                'EasyAuth: Set easyauth.privateBrowser.command in settings first.',
                'Open Settings'
            ).then(sel => {
                if (sel === 'Open Settings') {
                    void vscode.commands.executeCommand('easyauth.openSettings');
                }
            });
            return;
        }
        // Guard the URL itself: only http/https with a plain hostname (letters,
        // digits, dots, hyphens, or a bracketed IPv6 literal) and a safe path is
        // acceptable. This prevents shell metacharacters from reaching cmd.exe
        // via a malformed URL.
        if (!/^https?:\/\/(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?(\/[A-Za-z0-9._/-]*)?$/.test(url)) {
            outputChannel.error(`[extension] Private browser launch blocked: unsafe URL: ${url}`);
            void vscode.window.showErrorMessage('EasyAuth: Internal error: unsafe URL rejected.');
            return;
        }

        const parts = cmd.split(/\s+/);

        // Allowlist: letters, digits, hyphens, underscores, dots, slashes, colons (Windows paths).
        // Rejects shell metacharacters (&, |, ;, $, `, etc.) that could inject commands.
        const tokenOk = /^[A-Za-z0-9_.\\/:~-]+$/;
        const badToken = parts.find(t => !tokenOk.test(t));
        if (badToken) {
            void vscode.window.showErrorMessage(
                `EasyAuth: Unsafe character in privateBrowser.command token "${badToken}". Allowed: letters, digits, - _ . / \\ : ~`
            );
            return;
        }

        outputChannel.info(`[extension] Opening private browser: ${cmd} ${url}`);

        // On Windows use `cmd /c start` so App Paths (msedge, chrome, etc.) are resolved
        // via ShellExecuteEx — they are not on PATH and cp.spawn cannot find them directly.
        // Tokens are validated above so no shell metacharacters can reach cmd.exe.
        // On POSIX, `--` separates options from the URL to prevent flag-smuggling.
        const proc = process.platform === 'win32'
            ? cp.spawn('cmd', ['/c', 'start', '', ...parts, url], { detached: true, stdio: 'ignore' })
            : cp.spawn(parts[0], [...parts.slice(1), '--', url], { detached: true, stdio: 'ignore' });

        proc.on('error', (err) => {
            outputChannel.error(`[extension] Private browser spawn error: ${err.message}`);
            void vscode.window.showErrorMessage(`EasyAuth: Failed to launch browser: ${err.message}`);
        });
        proc.on('close', (code) => {
            if (typeof code === 'number' && code !== 0) {
                outputChannel.error(`[extension] Private browser exited with code ${code}`);
                void vscode.window.showErrorMessage(
                    `EasyAuth: Private browser command failed (exit ${code}). Check easyauth.privateBrowser.command.`
                );
            }
        });
        proc.unref();
    }

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.openInPrivateBrowser', async () => {
            if (vscode.env.remoteName !== undefined) {
                await copyGatewayUrlForPrivateBrowser('');
                return;
            }
            const port = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
            if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) {
                void vscode.window.showErrorMessage('EasyAuth: invalid listen port in configuration.');
                return;
            }
            launchInPrivateBrowser(gatewayUrl('', port));
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.start', async () => {
            if (emulator.isManaging()) {
                vscode.window.showInformationMessage('EasyAuth Emulator is already running.');
                return;
            }
            portDetector.resetForNewSession();
            const port = await portDetector.detectManual(context.workspaceState);
            if (port !== null) {
                outputShownSinceError = false;
                const sessionId = vscode.debug.activeDebugSession?.id ?? '__manual__';
                await emulator.start(port, sessionId);
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.stop', async () => {
            await emulator.stop();
            void emulator.updateStateForSecrets();
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.restart', async () => {
            await emulator.restart();
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.openOutput', () => {
            outputChannel.show();
        })
    );

    // Resolves the gateway URL the client's browser should use, preserving
    // the origin configured in site.url (https, test.localhost, a tunnel
    // domain, …) — cookies and TLS certificates are bound to that origin, and
    // loopback hosts resolve to the forwarded tunnel on the client anyway.
    // In a remote session, returns null with an error when VS Code forwarded
    // site.port to a different local port — new OAuth logins would fail there
    // (the IdP redirects the browser back to an origin that no longer reaches
    // the gateway).
    async function resolveGatewayBrowserUrl(urlPath: string): Promise<vscode.Uri | null> {
        const port = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
        if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) {
            void vscode.window.showErrorMessage('EasyAuth: invalid listen port in configuration.');
            return null;
        }
        const targetUri = vscode.Uri.parse(gatewayUrl(urlPath, port));
        if (!forwardingCheckApplies()) {
            return targetUri;
        }
        try {
            const check = await checkPortForwarding(port);
            switch (check.kind) {
                case 'match':
                    return targetUri;
                case 'mismatch':
                    void vscode.window.showErrorMessage(
                        `EasyAuth: cannot open the browser — port ${port} is forwarded to a different local port ` +
                        `(${check.localPort}), so OAuth login would fail. Fix: 1) In the PORTS panel, stop forwarding ` +
                        `port ${port}. 2) Quit the app using port ${port} on your PC, or change the easyauth.site.port ` +
                        `setting. Then try again.`
                    );
                    return null;
                case 'external-domain': {
                    // Use the forwarded URL — the site works there; sign-in
                    // needs the callback URL registered for that origin.
                    const origin = `${check.externalUri.scheme}://${check.externalUri.authority}`;
                    void vscode.window.showInformationMessage(
                        `EasyAuth: using the forwarded URL ${origin}. To sign in there, add ` +
                        `${origin}/oauth2/callback to your IdP app registration's redirect URIs.`
                    );
                    return check.externalUri.with({
                        path: check.externalUri.path.replace(/\/$/, '') + urlPath,
                    });
                }
                case 'unknown':
                    return targetUri;
            }
        } catch (err) {
            outputChannel.warn(`[extension] Port forwarding check failed: ${err}`);
            return targetUri;
        }
    }

    async function openGatewayInBrowser(urlPath: string): Promise<void> {
        const uri = await resolveGatewayBrowserUrl(urlPath);
        if (uri) {
            void vscode.env.openExternal(uri);
        }
    }

    // Remote sessions cannot launch a browser on the client PC (cp.spawn runs
    // on the remote host), so the private-browser actions copy the URL for
    // manual pasting into a private/incognito window instead.
    async function copyGatewayUrlForPrivateBrowser(urlPath: string): Promise<void> {
        const uri = await resolveGatewayBrowserUrl(urlPath);
        if (!uri) {
            return;
        }
        await vscode.env.clipboard.writeText(uri.toString(true));
        void vscode.window.showInformationMessage(
            'EasyAuth: URL copied to the clipboard — paste it into a private/incognito browser window ' +
            '(a private browser cannot be launched on your PC from a remote session).'
        );
    }

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.idp.openInBrowser', async (item: IdpTreeItem) => {
            await openGatewayInBrowser(`/.auth/login/${item.idpKey}`);
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.idp.openInPrivateBrowser', async (item: IdpTreeItem) => {
            if (vscode.env.remoteName !== undefined) {
                await copyGatewayUrlForPrivateBrowser(`/.auth/login/${item.idpKey}`);
                return;
            }
            const port = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
            if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) {
                void vscode.window.showErrorMessage('EasyAuth: invalid listen port in configuration.');
                return;
            }
            launchInPrivateBrowser(gatewayUrl(`/.auth/login/${item.idpKey}`, port));
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.copyPrivateBrowserUrl', async () => {
            await copyGatewayUrlForPrivateBrowser('');
        }),
        vscode.commands.registerCommand('easyauth.idp.copyPrivateBrowserUrl', async (item: IdpTreeItem) => {
            await copyGatewayUrlForPrivateBrowser(`/.auth/login/${item.idpKey}`);
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.openInBrowser', async () => {
            await openGatewayInBrowser('');
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.setSecret', async () => {
            await secretManager.runSetSecretCommand();
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.clearSecret', async () => {
            await secretManager.runClearSecretCommand();
        })
    );
}

export function deactivate(): Thenable<void> | undefined {
    // EmulatorManager.dispose() handles cleanup via context.subscriptions
    return undefined;
}

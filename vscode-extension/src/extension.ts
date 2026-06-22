import * as vscode from 'vscode';
import * as cp from 'child_process';
import { EmulatorManager } from './emulatorManager';
import { PortDetector } from './portDetector';
import { StatusBarManager } from './statusBar';
import { SecretManager } from './secretManager';
import { EmulatorTreeProvider } from './emulatorTreeProvider';

export function activate(context: vscode.ExtensionContext): void {
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

    context.subscriptions.push(
        treeProvider,
        treeView,
        emulator.onDidChangeState(state => {
            treeProvider.update(state);
            treeView.description = state;
        }),
        vscode.workspace.onDidChangeConfiguration(e => {
            if (e.affectsConfiguration('easyauth')) {
                updatePrivateBrowserContext();
                void updateSecretContext();
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
        })
    );

    // Debug session started
    context.subscriptions.push(
        vscode.debug.onDidStartDebugSession(async (session) => {
            const config = vscode.workspace.getConfiguration('easyauth');
            if (!config.get<boolean>('autoStart', true)) return;
            if (!emulator.hasConfig()) return;
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

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.openInPrivateBrowser', () => {
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
            const port = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
            if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) {
                void vscode.window.showErrorMessage('EasyAuth: invalid listen port in configuration.');
                return;
            }
            const url = `http://localhost:${port}`;
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

    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.openInBrowser', () => {
            const port = vscode.workspace.getConfiguration('easyauth').get<number>('site.port', 8080);
            if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) {
                void vscode.window.showErrorMessage('EasyAuth: invalid listen port in configuration.');
                return;
            }
            void vscode.env.openExternal(vscode.Uri.parse(`http://localhost:${port}`));
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

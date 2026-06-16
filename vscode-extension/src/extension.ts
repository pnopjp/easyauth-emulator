import * as vscode from 'vscode';
import { EmulatorManager } from './emulatorManager';
import { PortDetector } from './portDetector';
import { StatusBarManager } from './statusBar';
import { SecretManager } from './secretManager';

export function activate(context: vscode.ExtensionContext): void {
    const outputChannel = vscode.window.createOutputChannel('EasyAuth Emulator');
    const statusBar = new StatusBarManager();
    const secretManager = new SecretManager(context);
    const emulator = new EmulatorManager(context, outputChannel, statusBar, secretManager);
    const portDetector = new PortDetector(outputChannel);

    context.subscriptions.push(outputChannel, statusBar, emulator);

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
        })
    );

    // Commands
    context.subscriptions.push(
        vscode.commands.registerCommand('easyauth.statusBarClick', async () => {
            const state = emulator.getState();
            switch (state) {
                case 'unconfigured':
                    await vscode.commands.executeCommand('workbench.action.openSettings', '@ext:easyauth.easyauth-emulator');
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

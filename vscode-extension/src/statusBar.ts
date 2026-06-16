import * as vscode from 'vscode';

export type EmulatorState = 'stopped' | 'unconfigured' | 'starting' | 'running' | 'error';

export class StatusBarManager implements vscode.Disposable {
    private readonly item: vscode.StatusBarItem;

    constructor() {
        this.item = vscode.window.createStatusBarItem('easyauth.status', vscode.StatusBarAlignment.Left, 100);
        this.item.name = 'EasyAuth Emulator';
        this.item.command = 'easyauth.statusBarClick';
        this.item.show();
        this.update('stopped', null, null);
    }

    update(state: EmulatorState, listenPort: number | null, upstreamPort: number | null): void {
        this.item.command = 'easyauth.statusBarClick';
        this.item.backgroundColor = undefined;

        switch (state) {
            case 'stopped':
                this.item.text = '$(shield) EasyAuth: stopped';
                this.item.tooltip = 'EasyAuth Emulator is stopped. Click to start.';
                break;
            case 'unconfigured':
                this.item.text = '$(warning) EasyAuth: no config';
                this.item.tooltip = 'EasyAuth Emulator: no config. Click to open Settings.';
                break;
            case 'starting':
                this.item.text = '$(sync~spin) EasyAuth: starting...';
                this.item.tooltip = 'EasyAuth Emulator is starting... Click to open output.';
                break;
            case 'running': {
                const ports = listenPort && upstreamPort ? `${listenPort}:${upstreamPort}` : null;
                this.item.text = ports ? `$(shield) EasyAuth: ${ports}` : '$(shield) EasyAuth: running';
                this.item.tooltip = listenPort
                    ? `EasyAuth Emulator is running (listen: ${listenPort}, upstream: ${upstreamPort}). Click to open in browser.`
                    : 'EasyAuth Emulator is running. Click to open in browser.';
                break;
            }
            case 'error':
                this.item.text = '$(error) EasyAuth: error';
                this.item.tooltip = 'EasyAuth Emulator encountered an error. Click to open output.';
                this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
                break;
        }
    }

    dispose(): void {
        this.item.dispose();
    }
}

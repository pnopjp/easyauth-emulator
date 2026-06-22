import * as vscode from 'vscode';
import { EmulatorState } from './statusBar';

class EmulatorStatusItem extends vscode.TreeItem {
    constructor(state: EmulatorState) {
        super('EasyAuth Emulator', vscode.TreeItemCollapsibleState.None);
        this.contextValue = `easyauth.${state}`;
        this.iconPath = EmulatorStatusItem.iconFor(state);
        this.description = EmulatorStatusItem.labelFor(state);
    }

    private static iconFor(state: EmulatorState): vscode.ThemeIcon {
        switch (state) {
            case 'running':      return new vscode.ThemeIcon('shield', new vscode.ThemeColor('testing.iconPassed'));
            case 'starting':     return new vscode.ThemeIcon('sync~spin');
            case 'error':        return new vscode.ThemeIcon('error');
            case 'unconfigured':    return new vscode.ThemeIcon('warning');
            case 'missing_secret':       return new vscode.ThemeIcon('lock');
            case 'missing_entra_issuer': return new vscode.ThemeIcon('warning');
            default:                     return new vscode.ThemeIcon('shield');
        }
    }

    private static labelFor(state: EmulatorState): string {
        switch (state) {
            case 'running':      return 'running';
            case 'starting':     return 'starting...';
            case 'error':        return 'error';
            case 'unconfigured':    return 'not configured';
            case 'missing_secret':       return 'secret missing';
            case 'missing_entra_issuer': return 'Entra issuer missing';
            default:                     return 'stopped';
        }
    }
}

export class EmulatorTreeProvider implements vscode.TreeDataProvider<EmulatorStatusItem>, vscode.Disposable {
    private readonly _onDidChangeTreeData = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData: vscode.Event<void> = this._onDidChangeTreeData.event;

    private state: EmulatorState = 'stopped';

    update(state: EmulatorState): void {
        this.state = state;
        this._onDidChangeTreeData.fire();
    }

    getTreeItem(element: EmulatorStatusItem): vscode.TreeItem {
        return element;
    }

    getChildren(): EmulatorStatusItem[] {
        return [new EmulatorStatusItem(this.state)];
    }

    dispose(): void {
        this._onDidChangeTreeData.dispose();
    }
}

import * as vscode from 'vscode';
import { EmulatorState } from './statusBar';

export interface IdpInfo {
    key: string;
    displayName: string;
    icon?: string;
}

export class IdpTreeItem extends vscode.TreeItem {
    constructor(readonly idpKey: string, displayName: string, resolvedIcon: vscode.ThemeIcon | vscode.Uri) {
        super(displayName, vscode.TreeItemCollapsibleState.None);
        this.contextValue = 'easyauth.idp';
        this.iconPath = resolvedIcon;
    }
}

class EmulatorStatusItem extends vscode.TreeItem {
    constructor(state: EmulatorState, hasChildren: boolean, runSessionId: number) {
        super(
            'EasyAuth Emulator',
            hasChildren ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None
        );
        // Unique ID per run prevents VS Code from restoring a previously-collapsed state.
        this.id = hasChildren ? `easyauth-root-run-${runSessionId}` : 'easyauth-root';
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

type TreeNode = EmulatorStatusItem | IdpTreeItem;

const FALLBACK_ICON = new vscode.ThemeIcon('account');

export class EmulatorTreeProvider implements vscode.TreeDataProvider<TreeNode>, vscode.Disposable {
    private readonly _onDidChangeTreeData = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData: vscode.Event<void> = this._onDidChangeTreeData.event;

    private state: EmulatorState = 'stopped';
    private idps: IdpInfo[] = [];
    private iconsMode: string = 'simple';
    private runSessionId: number = 0;

    update(state: EmulatorState): void {
        if (state === 'running' && this.state !== 'running') {
            this.runSessionId++;
        }
        this.state = state;
        this._onDidChangeTreeData.fire();
    }

    updateIdps(idps: IdpInfo[], iconsMode: string): void {
        this.idps = idps;
        this.iconsMode = iconsMode;
        this._onDidChangeTreeData.fire();
    }

    private resolveIcon(idp: IdpInfo): vscode.ThemeIcon | vscode.Uri {
        if (this.iconsMode === 'text' || this.iconsMode === 'generic' || !idp.icon) {
            return FALLBACK_ICON;
        }
        if (/^https?:\/\//.test(idp.icon)) {
            return vscode.Uri.parse(idp.icon);
        }
        // Simple Icons slug
        return vscode.Uri.parse(`https://cdn.simpleicons.org/${encodeURIComponent(idp.icon)}`);
    }

    getTreeItem(element: TreeNode): vscode.TreeItem {
        return element;
    }

    getChildren(element?: TreeNode): TreeNode[] {
        if (!element) {
            const hasChildren = this.state === 'running' && this.idps.length > 0;
            return [new EmulatorStatusItem(this.state, hasChildren, this.runSessionId)];
        }
        if (element instanceof EmulatorStatusItem && this.state === 'running') {
            return this.idps.map(idp => new IdpTreeItem(idp.key, idp.displayName, this.resolveIcon(idp)));
        }
        return [];
    }

    dispose(): void {
        this._onDidChangeTreeData.dispose();
    }
}

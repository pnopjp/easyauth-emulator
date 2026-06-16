import * as vscode from 'vscode';

const BUILTIN_IDPS = [
    { key: 'entra',    label: 'Microsoft Entra' },
    { key: 'google',   label: 'Google' },
    { key: 'facebook', label: 'Facebook' },
    { key: 'apple',    label: 'Apple' },
    { key: 'github',   label: 'GitHub' },
] as const;

interface IdpItem extends vscode.QuickPickItem {
    idpKey: string;
}

export class SecretManager {
    constructor(private readonly context: vscode.ExtensionContext) {}

    private wsPrefix(): string {
        const ws = vscode.workspace.workspaceFolders?.[0]?.uri.toString() ?? '__global__';
        return `easyauth|${ws}`;
    }

    private storageKey(idpKey: string): string {
        return `${this.wsPrefix()}|${idpKey}`;
    }

    async get(idpKey: string): Promise<string | undefined> {
        return this.context.secrets.get(this.storageKey(idpKey));
    }

    async set(idpKey: string, value: string): Promise<void> {
        return this.context.secrets.store(this.storageKey(idpKey), value);
    }

    async delete(idpKey: string): Promise<void> {
        return this.context.secrets.delete(this.storageKey(idpKey));
    }

    // IDPs that have clientId configured — shown in "Set Client Secret"
    private getConfiguredIdpItems(): IdpItem[] {
        const config = vscode.workspace.getConfiguration('easyauth');
        const items: IdpItem[] = [];

        for (const { key, label } of BUILTIN_IDPS) {
            if (config.get<string>(`${key}.clientId`, '').trim()) {
                items.push({ label, idpKey: key });
            }
        }

        const customs = config.get<Array<{ name?: string; clientId?: string }>>('customIdps', []);
        for (const c of customs) {
            const name = c.name?.trim();
            if (name && c.clientId?.trim()) {
                items.push({ label: name, description: 'custom', idpKey: `custom:${name}` });
            }
        }

        return items;
    }

    // IDPs that have a secret stored — shown in "Clear Client Secret"
    private async getIdpsWithSecrets(): Promise<IdpItem[]> {
        const config = vscode.workspace.getConfiguration('easyauth');
        const candidates: IdpItem[] = [];

        for (const { key, label } of BUILTIN_IDPS) {
            candidates.push({ label, idpKey: key });
        }

        const customs = config.get<Array<{ name?: string }>>('customIdps', []);
        for (const c of customs) {
            const name = c.name?.trim();
            if (name) {
                candidates.push({ label: name, description: 'custom', idpKey: `custom:${name}` });
            }
        }

        const result: IdpItem[] = [];
        for (const item of candidates) {
            const val = await this.context.secrets.get(this.storageKey(item.idpKey));
            if (val) result.push(item);
        }
        return result;
    }

    async runSetSecretCommand(): Promise<void> {
        const items = this.getConfiguredIdpItems();
        if (items.length === 0) {
            vscode.window.showWarningMessage(
                'No IDPs with Client ID configured. Set a Client ID in settings first.'
            );
            return;
        }

        const picked = await vscode.window.showQuickPick(items, { placeHolder: 'Select IDP' });
        if (!picked) return;

        const secret = await vscode.window.showInputBox({
            prompt: `Enter client secret for ${picked.label}`,
            password: true,
            ignoreFocusOut: true,
        });
        if (secret === undefined) return;
        if (!secret.trim()) {
            vscode.window.showWarningMessage('Client secret cannot be empty.');
            return;
        }

        await this.set(picked.idpKey, secret.trim());
        void vscode.window.showInformationMessage(`Client secret for ${picked.label} saved.`);
    }

    async getCookieSecret(): Promise<string> {
        const key = `${this.wsPrefix()}|__cookieSecret__`;
        let secret = await this.context.secrets.get(key);
        if (!secret) {
            const { randomBytes } = await import('crypto');
            secret = randomBytes(16).toString('base64');
            await this.context.secrets.store(key, secret);
        }
        return secret;
    }

    async runClearSecretCommand(): Promise<void> {
        const items = await this.getIdpsWithSecrets();
        if (items.length === 0) {
            vscode.window.showInformationMessage('No client secrets stored for this workspace.');
            return;
        }

        const picked = await vscode.window.showQuickPick(items, { placeHolder: 'Select IDP to clear secret' });
        if (!picked) return;

        await this.delete(picked.idpKey);
        void vscode.window.showInformationMessage(`Client secret for ${picked.label} cleared.`);
    }
}

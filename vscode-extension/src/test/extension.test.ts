import * as assert from 'assert';
import * as vscode from 'vscode';

const EXTENSION_ID = 'pnop.easyauth-emulator';

suite('Extension', () => {
    suiteSetup(async () => {
        const ext = vscode.extensions.getExtension(EXTENSION_ID);
        await ext?.activate();
    });

    test('extension is present', () => {
        assert.ok(
            vscode.extensions.getExtension(EXTENSION_ID),
            `Extension "${EXTENSION_ID}" should be installed`,
        );
    });

    test('extension is active after activation', () => {
        const ext = vscode.extensions.getExtension(EXTENSION_ID);
        assert.ok(ext?.isActive, 'Extension should be active');
    });

    test('all commands are registered', async () => {
        const registered = await vscode.commands.getCommands();
        const expected = [
            'easyauth.start',
            'easyauth.stop',
            'easyauth.restart',
            'easyauth.openInBrowser',
            'easyauth.openInPrivateBrowser',
            'easyauth.setSecret',
            'easyauth.clearSecret',
            'easyauth.openOutput',
            'easyauth.openSettings',
            'easyauth.idp.openInBrowser',
            'easyauth.idp.openInPrivateBrowser',
            'easyauth.copyPrivateBrowserUrl',
            'easyauth.idp.copyPrivateBrowserUrl',
        ];
        for (const cmd of expected) {
            assert.ok(registered.includes(cmd), `Command "${cmd}" should be registered`);
        }
    });
});

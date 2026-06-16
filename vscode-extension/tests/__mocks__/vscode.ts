import { vi } from 'vitest';

export const window = {
    showQuickPick: vi.fn(),
    showInputBox: vi.fn(),
};

export const workspace = {
    getConfiguration: vi.fn().mockReturnValue({
        get: vi.fn().mockReturnValue(null),
    }),
    workspaceFolders: [] as unknown[],
};

export const OutputChannel = {};

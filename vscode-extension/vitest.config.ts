import { defineConfig } from 'vitest/config';
import path from 'path';

export default defineConfig({
    resolve: {
        alias: {
            vscode: path.resolve(__dirname, 'tests/__mocks__/vscode.ts'),
        },
    },
    test: {
        include: ['tests/**/*.test.ts'],
    },
});

import * as vscode from 'vscode';
import * as fs from 'fs';
import * as net from 'net';
import * as path from 'path';

type Framework = 'azure-functions' | '.net' | 'java' | 'nodejs' | 'python' | 'unknown';

const FRAMEWORK_CACHE_KEY_PREFIX = 'easyauth.framework.';

// Stdout patterns keyed by framework
const STDOUT_PATTERNS: RegExp[] = [
    /Now listening on: https?:\/\/[^:]+:(\d+)/,
    /Tomcat started on port[s]?\s+(\d+)/,
    /listening on.*?port\s+(\d+)/i,
    /Running on http:\/\/[^:]+:(\d+)/,
    /Uvicorn running on https?:\/\/[^:]+:(\d+)/,
    /[Ll]istening on https?:\/\/[^:]+:(\d+)/,
    /Local URL:\s*https?:\/\/[^:]+:(\d+)/, // Streamlit
    /Node\.js is listening to PORT:(\d+)/,
];

/** Thrown when the user configured launchJsonPortKey/stdoutPortPattern but neither matched. */
export class CustomDetectionFailedError extends Error {
    constructor(public readonly detail: string) {
        super(`Custom port detection configured but found no match (${detail})`);
        this.name = 'CustomDetectionFailedError';
    }
}

export class PortDetector {
    private stdoutPort: number | null = null;
    private portWaiters: Array<(port: number) => void> = [];
    private textListeners: Array<(text: string) => void> = [];

    constructor(private readonly outputChannel: vscode.OutputChannel) {}

    private log(message: string): void {
        this.outputChannel.appendLine(message);
    }

    private feed(text: string): void {
        const port = this.extractPortFromText(text);
        if (port !== null && this.stdoutPort === null) {
            this.stdoutPort = port;
            const waiters = this.portWaiters.splice(0);
            for (const resolve of waiters) resolve(port);
        }
        for (const listener of this.textListeners) listener(text);
    }

    /** Called for every terminal shell execution (VS Code 1.93+). */
    onShellExecution(execution: vscode.TerminalShellExecution): void {
        void (async () => {
            for await (const data of execution.read()) {
                this.feed(data);
            }
        })();
    }

    /** Called by the DebugAdapterTracker for every output event. */
    onDebugOutput(text: string): void {
        this.feed(text);
    }

    resetForNewSession(): void {
        this.stdoutPort = null;
        this.portWaiters = [];
        this.textListeners = [];
    }

    async detect(session: vscode.DebugSession, workspaceState: vscode.Memento): Promise<number | null> {
        const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

        // Step 1: explicit setting
        const config = vscode.workspace.getConfiguration('easyauth');
        const manual = config.get<number | null>('upstreamPort', null);
        if (manual !== null) {
            this.log(`[portDetector] Step 1: using configured upstreamPort ${manual}`);
            return manual;
        }

        // Step 2: user-specified detection (launchJsonPortKey / stdoutPortPattern).
        // If either is configured, only these are tried — a non-match throws instead
        // of falling through to the built-in steps below.
        const customKey = config.get<string>('launchJsonPortKey', '').trim();
        const customPatternStr = config.get<string>('stdoutPortPattern', '').trim();
        if (customKey || customPatternStr) {
            const customPort = await this.fromCustomDetection(session, customKey, customPatternStr, config);
            if (customPort !== null) return customPort;
            const detail = [
                customKey ? `launchJsonPortKey=${customKey}` : null,
                customPatternStr ? `stdoutPortPattern=${customPatternStr}` : null,
            ].filter(Boolean).join(', ');
            this.log(`[portDetector] Step 2: custom detection configured but found no match (${detail})`);
            throw new CustomDetectionFailedError(detail);
        }

        // Step 3: launch.json
        const launchPort = this.fromLaunchJson(session);

        // Step 4: framework config file
        const framework = root ? this.detectFramework(root, workspaceState) : 'unknown';
        const configPort = root ? this.fromConfigFile(root, framework) : null;

        if (launchPort !== null) {
            this.log(`[portDetector] Step 3: detected port ${launchPort} from launch.json`);
            return launchPort;
        }
        if (configPort !== null) {
            this.log(`[portDetector] Step 4: detected port ${configPort} from ${framework} config file`);
            return configPort;
        }

        // Step 5: stdout (built-in patterns)
        const stdoutTimeoutMs = config.get<number>('stdoutPatternTimeoutMs', 3_000);
        this.log(`[portDetector] Step 5: waiting up to ${stdoutTimeoutMs}ms for port in debug output...`);
        const stdoutPort = await this.waitForStdoutPort(stdoutTimeoutMs);
        if (stdoutPort !== null) {
            this.log(`[portDetector] Step 5: detected port ${stdoutPort} from stdout`);
            return stdoutPort;
        }

        // Step 6: port scan
        const hint = config.get<number | null>('portScanBase', null);
        if (hint !== null) {
            this.log(`[portDetector] Step 6: scanning ports from ${hint}`);
            const max = config.get<number>('portScanMax', 5);
            const candidates = await this.scan(hint, max);
            if (candidates.length === 1) {
                this.log(`[portDetector] Step 6: found port ${candidates[0]} via scan`);
                return candidates[0];
            }
            if (candidates.length > 1) return this.pickFromList(candidates);
        } else {
            // When no portScanBase is configured, scan well-known default ports used across
            // common frameworks (Flask:5000, Django/uvicorn:8000, Spring Boot/general:8080,
            // Express/Next.js:3000). Exclude the emulator's own port to avoid self-detection.
            const emulatorPort = config.get<number>('site.port', 8080);
            const wellKnown = [3000, 5000, 7071, 8000, 8080].filter(p => p !== emulatorPort);
            this.log(`[portDetector] Step 6: scanning well-known ports ${wellKnown.join(', ')} (excluding emulator port ${emulatorPort})`);
            const candidates: number[] = [];
            for (const p of wellKnown) {
                if (await this.isListening(p)) candidates.push(p);
            }
            if (candidates.length === 1) {
                this.log(`[portDetector] Step 6: found port ${candidates[0]}`);
                return candidates[0];
            }
            if (candidates.length > 1) return this.pickFromList(candidates);
        }

        // Step 7: manual input
        return this.promptInput();
    }

    // ------------------------------------------------------------------ //
    //  Step 2: user-specified detection
    // ------------------------------------------------------------------ //
    private async fromCustomDetection(
        session: vscode.DebugSession,
        key: string,
        patternStr: string,
        config: vscode.WorkspaceConfiguration,
    ): Promise<number | null> {
        if (key) {
            const port = this.fromCustomLaunchJsonKey(session, key);
            if (port !== null) {
                this.log(`[portDetector] Step 2: detected port ${port} from custom launch.json key`);
                return port;
            }
        }
        if (patternStr) {
            let pattern: RegExp;
            try {
                pattern = new RegExp(patternStr);
            } catch (err) {
                this.log(`[portDetector] Step 2: invalid easyauth.stdoutPortPattern: ${err}`);
                return null;
            }
            const timeoutMs = config.get<number>('stdoutPatternTimeoutMs', 3_000);
            this.log(`[portDetector] Step 2: waiting up to ${timeoutMs}ms for custom stdout pattern...`);
            const port = await this.waitForCustomStdoutPort(pattern, timeoutMs);
            if (port !== null) {
                this.log(`[portDetector] Step 2: detected port ${port} from custom stdout pattern`);
                return port;
            }
        }
        return null;
    }

    // Probes the same three locations the built-in detection reads from — env →
    // top-level config field → args flag — in that order, for the one given key.
    private fromCustomLaunchJsonKey(session: vscode.DebugSession, key: string): number | null {
        return this.forEachMatchingLaunchConfig(session, cfg => {
            const env = (cfg['env'] ?? {}) as Record<string, string>;

            const envValue = env[key];
            if (envValue) {
                const n = parseInt(envValue, 10);
                if (!isNaN(n)) return n;
                const p = this.portFromUrlList(envValue);
                if (p !== null) return p;
            }

            if (typeof cfg[key] === 'string') {
                const p = this.portFromUrlList(cfg[key] as string);
                if (p !== null) return p;
            }

            const args = Array.isArray(cfg['args']) ? (cfg['args'] as unknown[]).map(String) : [];
            for (let i = 0; i < args.length - 1; i++) {
                if (args[i] !== key) continue;
                const m = args[i + 1].match(/:?(\d+)$/);
                if (m) {
                    const n = parseInt(m[1], 10);
                    if (!isNaN(n)) return n;
                }
            }
            return null;
        });
    }

    private waitForCustomStdoutPort(pattern: RegExp, timeoutMs: number): Promise<number | null> {
        return new Promise<number | null>((resolve) => {
            const timer = setTimeout(() => {
                this.textListeners = this.textListeners.filter(l => l !== onText);
                resolve(null);
            }, timeoutMs);

            const onText = (text: string): void => {
                const m = text.match(pattern);
                const n = m ? parseInt(m[1], 10) : NaN;
                if (!isNaN(n)) {
                    clearTimeout(timer);
                    this.textListeners = this.textListeners.filter(l => l !== onText);
                    resolve(n);
                }
            };
            this.textListeners.push(onText);
        });
    }

    // Used when started via command palette (no debug session)
    async detectManual(workspaceState: vscode.Memento): Promise<number | null> {
        const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        const config = vscode.workspace.getConfiguration('easyauth');
        const manual = config.get<number | null>('upstreamPort', null);
        if (manual !== null) return manual;

        if (root) {
            const framework = this.detectFramework(root, workspaceState);
            const configPort = this.fromConfigFile(root, framework);
            if (configPort !== null) return configPort;
        }

        return this.promptInput();
    }

    // ------------------------------------------------------------------ //
    //  Step 3: launch.json
    // ------------------------------------------------------------------ //
    private fromLaunchJson(session: vscode.DebugSession): number | null {
        return this.forEachMatchingLaunchConfig(session, cfg => this.portFromLaunchConfig(cfg));
    }

    /** Reads .vscode/launch.json and runs `extract` over each configuration matching the session. */
    private forEachMatchingLaunchConfig(
        session: vscode.DebugSession,
        extract: (cfg: Record<string, unknown>) => number | null,
    ): number | null {
        for (const folder of vscode.workspace.workspaceFolders ?? []) {
            const p = path.join(folder.uri.fsPath, '.vscode', 'launch.json');
            if (!fs.existsSync(p)) continue;
            try {
                const text = fs.readFileSync(p, 'utf-8')
                    .replace(/\/\/[^\n]*/g, '')
                    .replace(/\/\*[\s\S]*?\*\//g, '');
                const json = JSON.parse(text) as { configurations?: unknown[] };
                for (const cfg of (json.configurations ?? []) as Record<string, unknown>[]) {
                    // Match by name when multiple configs exist
                    if ((json.configurations?.length ?? 0) > 1 && cfg['name'] !== session.name) continue;
                    const port = extract(cfg);
                    if (port !== null) return port;
                }
            } catch { /* ignore */ }
        }
        return null;
    }

    private portFromLaunchConfig(cfg: Record<string, unknown>): number | null {
        const env = (cfg['env'] ?? {}) as Record<string, string>;

        const directPort = env['PORT'] ?? env['port'];
        if (directPort) {
            const n = parseInt(directPort, 10);
            if (!isNaN(n)) return n;
        }

        // Flask-specific env vars
        const flaskPort = env['FLASK_RUN_PORT'] ?? env['FLASK_PORT'];
        if (flaskPort) {
            const n = parseInt(flaskPort, 10);
            if (!isNaN(n)) return n;
        }

        if (env['ASPNETCORE_URLS']) {
            const p = this.portFromUrlList(env['ASPNETCORE_URLS']);
            if (p !== null) return p;
        }
        if (env['ASPNETCORE_HTTP_PORTS']) {
            const n = parseInt(env['ASPNETCORE_HTTP_PORTS'].split(';')[0], 10);
            if (!isNaN(n)) return n;
        }
        if (typeof cfg['applicationUrl'] === 'string') {
            const p = this.portFromUrlList(cfg['applicationUrl']);
            if (p !== null) return p;
        }

        // --port / -p / --bind / --server.port argument in args array
        // (Flask, uvicorn, gunicorn, Streamlit, etc.)
        const args = Array.isArray(cfg['args']) ? (cfg['args'] as unknown[]).map(String) : [];
        for (let i = 0; i < args.length - 1; i++) {
            if (args[i] === '--port' || args[i] === '-p' || args[i] === '--bind' || args[i] === '--server.port') {
                // --bind may be "0.0.0.0:8000"
                const raw = args[i + 1];
                const m = raw.match(/:?(\d+)$/);
                if (m) {
                    const n = parseInt(m[1], 10);
                    if (!isNaN(n)) return n;
                }
            }
        }

        return null;
    }

    // http:// preferred, then first URL
    private portFromUrlList(urlStr: string): number | null {
        const urls = urlStr.split(';').map(u => u.trim()).filter(Boolean);
        const httpUrls = urls.filter(u => u.startsWith('http://'));
        for (const url of (httpUrls.length > 0 ? httpUrls : urls)) {
            const m = url.match(/:(\d+)\/?$/);
            if (m) return parseInt(m[1], 10);
        }
        return null;
    }

    // ------------------------------------------------------------------ //
    //  Step 0: framework detection (cached per workspace)
    // ------------------------------------------------------------------ //
    private detectFramework(root: string, state: vscode.Memento): Framework {
        const key = FRAMEWORK_CACHE_KEY_PREFIX + root;
        const cached = state.get<Framework>(key);
        if (cached) return cached;

        let framework: Framework = 'unknown';
        try {
            const entries = fs.readdirSync(root);
            if (entries.includes('host.json')) {
                // Azure Functions project: host.json at workspace root
                framework = 'azure-functions';
            } else if (entries.some(f => f.endsWith('.csproj')) || fs.existsSync(path.join(root, 'Properties', 'launchSettings.json'))) {
                framework = '.net';
            } else if (fs.existsSync(path.join(root, 'pom.xml')) || fs.existsSync(path.join(root, 'build.gradle'))) {
                framework = 'java';
            } else if (entries.includes('package.json')) {
                framework = 'nodejs';
            } else if (
                entries.includes('requirements.txt') ||
                entries.includes('pyproject.toml') ||
                entries.some(f => f.endsWith('.py'))
            ) {
                framework = 'python';
            }
        } catch { /* ignore */ }

        void state.update(key, framework);
        return framework;
    }

    // ------------------------------------------------------------------ //
    //  Step 4: framework config file
    // ------------------------------------------------------------------ //
    private fromConfigFile(root: string, framework: Framework): number | null {
        try {
            switch (framework) {
                case 'azure-functions': {
                    const settingsPath = path.join(root, 'local.settings.json');
                    if (fs.existsSync(settingsPath)) {
                        const json = JSON.parse(fs.readFileSync(settingsPath, 'utf-8')) as {
                            Host?: { LocalHttpPort?: number };
                        };
                        if (typeof json.Host?.LocalHttpPort === 'number') return json.Host.LocalHttpPort;
                    }
                    return 7071; // Azure Functions Core Tools default port
                }
                case '.net': {
                    const p = path.join(root, 'Properties', 'launchSettings.json');
                    if (!fs.existsSync(p)) break;
                    const json = JSON.parse(fs.readFileSync(p, 'utf-8')) as {
                        profiles?: Record<string, { applicationUrl?: string }>;
                    };
                    for (const profile of Object.values(json.profiles ?? {})) {
                        if (typeof profile.applicationUrl === 'string') {
                            const port = this.portFromUrlList(profile.applicationUrl);
                            if (port !== null) return port;
                        }
                    }
                    break;
                }
                case 'java': {
                    const propsPath = path.join(root, 'src', 'main', 'resources', 'application.properties');
                    if (fs.existsSync(propsPath)) {
                        const m = fs.readFileSync(propsPath, 'utf-8').match(/^server\.port\s*=\s*(\d+)/m);
                        if (m) return parseInt(m[1], 10);
                    }
                    const ymlPath = path.join(root, 'src', 'main', 'resources', 'application.yml');
                    if (fs.existsSync(ymlPath)) {
                        const m = fs.readFileSync(ymlPath, 'utf-8').match(/server:\s*\n\s*port:\s*(\d+)/);
                        if (m) return parseInt(m[1], 10);
                    }
                    break;
                }
                case 'nodejs':
                case 'python': {
                    if (framework === 'python') {
                        const streamlitConfigPath = path.join(root, '.streamlit', 'config.toml');
                        if (fs.existsSync(streamlitConfigPath)) {
                            const content = fs.readFileSync(streamlitConfigPath, 'utf-8');
                            const serverSection = content.match(/\[server\]([\s\S]*?)(?:\n\[|$)/);
                            const m = serverSection?.[1].match(/^\s*port\s*=\s*(\d+)/m);
                            if (m) return parseInt(m[1], 10);
                        }
                    }
                    const envPath = path.join(root, '.env');
                    if (fs.existsSync(envPath)) {
                        const content = fs.readFileSync(envPath, 'utf-8');
                        // Match PORT, FLASK_RUN_PORT, or FLASK_PORT (Flask-specific)
                        const m = content.match(/^(?:FLASK_RUN_PORT|FLASK_PORT|PORT)\s*=\s*(\d+)/m);
                        if (m) return parseInt(m[1], 10);
                    }
                    break;
                }
            }
        } catch { /* ignore */ }
        return null;
    }

    // ------------------------------------------------------------------ //
    //  Step 5: stdout
    // ------------------------------------------------------------------ //
    private extractPortFromText(text: string): number | null {
        for (const re of STDOUT_PATTERNS) {
            const m = text.match(re);
            if (m) return parseInt(m[1], 10);
        }
        return null;
    }

    private waitForStdoutPort(timeoutMs: number): Promise<number | null> {
        if (this.stdoutPort !== null) return Promise.resolve(this.stdoutPort);
        return new Promise<number | null>((resolve) => {
            const timer = setTimeout(() => {
                this.portWaiters = this.portWaiters.filter(r => r !== onPort);
                resolve(null);
            }, timeoutMs);

            const onPort = (port: number): void => {
                clearTimeout(timer);
                resolve(port);
            };
            this.portWaiters.push(onPort);
        });
    }

    // ------------------------------------------------------------------ //
    //  Step 6: port scan
    // ------------------------------------------------------------------ //
    private async scan(basePort: number, maxPorts: number): Promise<number[]> {
        const results: number[] = [];
        for (let i = 0; i < maxPorts; i++) {
            if (await this.isListening(basePort + i)) results.push(basePort + i);
        }
        return results;
    }

    private isListening(port: number): Promise<boolean> {
        return new Promise(resolve => {
            const socket = new net.Socket();
            socket.setTimeout(300);
            socket.once('connect', () => { socket.destroy(); resolve(true); });
            socket.once('timeout', () => { socket.destroy(); resolve(false); });
            socket.once('error', () => resolve(false));
            socket.connect(port, '127.0.0.1');
        });
    }

    // ------------------------------------------------------------------ //
    //  Step 7: UI
    // ------------------------------------------------------------------ //
    private async pickFromList(ports: number[]): Promise<number | null> {
        const pick = await vscode.window.showQuickPick(
            ports.map(p => ({ label: String(p), description: `localhost:${p}` })),
            { placeHolder: 'Multiple ports detected — select the upstream port', title: 'EasyAuth Emulator' }
        );
        return pick ? parseInt(pick.label, 10) : null;
    }

    private async promptInput(): Promise<number | null> {
        const value = await vscode.window.showInputBox({
            title: 'EasyAuth Emulator: Enter Upstream Port',
            prompt: 'Enter the port your application listens on',
            placeHolder: '8080',
            validateInput: (v) => {
                const n = parseInt(v, 10);
                return isNaN(n) || n < 1 || n > 65535 ? 'Enter a valid port number (1–65535)' : null;
            },
        });
        return value ? parseInt(value, 10) : null;
    }

    /** Public entry point for the "custom detection failed" retry flow (§6 Step 7 UI, no fallback chain). */
    async promptManualPort(): Promise<number | null> {
        return this.promptInput();
    }
}

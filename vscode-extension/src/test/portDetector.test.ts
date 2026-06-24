import * as assert from 'assert';
import { PortDetector } from '../portDetector';

function makeDetector(): PortDetector {
    const mockChannel = { appendLine: (_: string) => {}, append: (_: string) => {} };
    return new PortDetector(mockChannel as any);
}

// ---------------------------------------------------------------------------
// extractPortFromText
// ---------------------------------------------------------------------------

suite('PortDetector: extractPortFromText', () => {
    let detector: PortDetector;

    setup(() => {
        detector = makeDetector();
    });

    const matchCases: [string, number][] = [
        // .NET (Kestrel)
        ['Now listening on: http://localhost:5000',           5000],
        ['Now listening on: https://0.0.0.0:5001',           5001],
        // Tomcat
        ['Tomcat started on port 8080',                      8080],
        ['Tomcat started on ports 8080 with context path /', 8080],
        // Generic (case-insensitive)
        ['listening on port 3000',                           3000],
        ['LISTENING ON PORT 3000',                           3000],
        // Flask
        ['Running on http://127.0.0.1:5000',                 5000],
        // Uvicorn
        ['Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)', 8000],
        ['Uvicorn running on https://0.0.0.0:8443',          8443],
    ];

    for (const [text, expected] of matchCases) {
        test(`"${text}" → ${expected}`, () => {
            assert.strictEqual((detector as any).extractPortFromText(text), expected);
        });
    }

    const nonMatchCases = ['', 'Hello world', 'port: no number here', 'server started successfully'];
    for (const text of nonMatchCases) {
        test(`returns null for non-matching: "${text}"`, () => {
            assert.strictEqual((detector as any).extractPortFromText(text), null);
        });
    }
});

// ---------------------------------------------------------------------------
// portFromUrlList
// ---------------------------------------------------------------------------

suite('PortDetector: portFromUrlList', () => {
    let detector: PortDetector;

    setup(() => {
        detector = makeDetector();
    });

    test('extracts port from a single HTTP URL', () => {
        assert.strictEqual((detector as any).portFromUrlList('http://localhost:5000'), 5000);
    });

    test('prefers HTTP over HTTPS in a semicolon-separated list', () => {
        assert.strictEqual(
            (detector as any).portFromUrlList('https://localhost:5001;http://localhost:5000'),
            5000,
        );
    });

    test('falls back to HTTPS when no HTTP URL is present', () => {
        assert.strictEqual((detector as any).portFromUrlList('https://localhost:5001'), 5001);
    });

    test('handles a trailing slash in the URL', () => {
        assert.strictEqual((detector as any).portFromUrlList('http://localhost:5000/'), 5000);
    });

    test('returns null for an empty string', () => {
        assert.strictEqual((detector as any).portFromUrlList(''), null);
    });

    test('returns null for a URL without an explicit port', () => {
        assert.strictEqual((detector as any).portFromUrlList('http://localhost'), null);
    });
});

// ---------------------------------------------------------------------------
// portFromLaunchConfig
// ---------------------------------------------------------------------------

suite('PortDetector: portFromLaunchConfig', () => {
    let detector: PortDetector;

    setup(() => {
        detector = makeDetector();
    });

    test('reads env.PORT', () => {
        assert.strictEqual(
            (detector as any).portFromLaunchConfig({ env: { PORT: '3000' } }),
            3000,
        );
    });

    test('reads env.port (lowercase key)', () => {
        assert.strictEqual(
            (detector as any).portFromLaunchConfig({ env: { port: '3001' } }),
            3001,
        );
    });

    test('reads ASPNETCORE_URLS and prefers the HTTP entry', () => {
        const cfg = { env: { ASPNETCORE_URLS: 'https://localhost:5001;http://localhost:5000' } };
        assert.strictEqual((detector as any).portFromLaunchConfig(cfg), 5000);
    });

    test('reads ASPNETCORE_HTTP_PORTS and uses the first value', () => {
        assert.strictEqual(
            (detector as any).portFromLaunchConfig({ env: { ASPNETCORE_HTTP_PORTS: '5001;5002' } }),
            5001,
        );
    });

    test('reads applicationUrl and prefers the HTTP entry', () => {
        const cfg = { applicationUrl: 'https://localhost:5001;http://localhost:5000' };
        assert.strictEqual((detector as any).portFromLaunchConfig(cfg), 5000);
    });

    test('returns null when nothing is configured', () => {
        assert.strictEqual((detector as any).portFromLaunchConfig({}), null);
    });
});

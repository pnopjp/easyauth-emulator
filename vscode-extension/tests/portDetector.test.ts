/**
 * Unit tests for the pure-logic methods of PortDetector.
 *
 * VS Code API calls are not involved: private methods are accessed via
 * `(instance as any).method(...)` to avoid coupling tests to the public API.
 *
 * Run:
 *     npm test
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { PortDetector } from '../src/portDetector';

function makeDetector(): PortDetector {
    const mockChannel = { appendLine: vi.fn(), append: vi.fn() };
    return new PortDetector(mockChannel as any);
}

// ---------------------------------------------------------------------------
// extractPortFromText
// ---------------------------------------------------------------------------

describe('extractPortFromText', () => {
    let detector: PortDetector;
    beforeEach(() => { detector = makeDetector(); });

    it.each([
        // .NET (Kestrel)
        ['Now listening on: http://localhost:5000',          5000],
        ['Now listening on: https://0.0.0.0:5001',          5001],
        // Tomcat
        ['Tomcat started on port 8080',                     8080],
        ['Tomcat started on ports 8080 with context path /', 8080],
        // Generic (case-insensitive)
        ['listening on port 3000',                          3000],
        ['LISTENING ON PORT 3000',                          3000],
        // Flask
        ['Running on http://127.0.0.1:5000',                5000],
        // Uvicorn
        ['Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)', 8000],
        ['Uvicorn running on https://0.0.0.0:8443',         8443],
    ] as [string, number][])('"%s" → %i', (text, expected) => {
        expect((detector as any).extractPortFromText(text)).toBe(expected);
    });

    it.each([
        '',
        'Hello world',
        'port: no number here',
        'server started successfully',
    ])('returns null for non-matching: "%s"', (text) => {
        expect((detector as any).extractPortFromText(text)).toBeNull();
    });
});

// ---------------------------------------------------------------------------
// portFromUrlList
// ---------------------------------------------------------------------------

describe('portFromUrlList', () => {
    let detector: PortDetector;
    beforeEach(() => { detector = makeDetector(); });

    it('extracts port from a single HTTP URL', () => {
        expect((detector as any).portFromUrlList('http://localhost:5000')).toBe(5000);
    });

    it('prefers HTTP over HTTPS in a semicolon-separated list', () => {
        expect((detector as any).portFromUrlList('https://localhost:5001;http://localhost:5000')).toBe(5000);
    });

    it('falls back to HTTPS when no HTTP URL is present', () => {
        expect((detector as any).portFromUrlList('https://localhost:5001')).toBe(5001);
    });

    it('handles a trailing slash in the URL', () => {
        expect((detector as any).portFromUrlList('http://localhost:5000/')).toBe(5000);
    });

    it('returns null for an empty string', () => {
        expect((detector as any).portFromUrlList('')).toBeNull();
    });

    it('returns null for a URL without an explicit port', () => {
        expect((detector as any).portFromUrlList('http://localhost')).toBeNull();
    });
});

// ---------------------------------------------------------------------------
// portFromLaunchConfig
// ---------------------------------------------------------------------------

describe('portFromLaunchConfig', () => {
    let detector: PortDetector;
    beforeEach(() => { detector = makeDetector(); });

    it('reads env.PORT', () => {
        expect((detector as any).portFromLaunchConfig({ env: { PORT: '3000' } })).toBe(3000);
    });

    it('reads env.port (lowercase key)', () => {
        expect((detector as any).portFromLaunchConfig({ env: { port: '3001' } })).toBe(3001);
    });

    it('reads ASPNETCORE_URLS and prefers the HTTP entry', () => {
        const cfg = { env: { ASPNETCORE_URLS: 'https://localhost:5001;http://localhost:5000' } };
        expect((detector as any).portFromLaunchConfig(cfg)).toBe(5000);
    });

    it('reads ASPNETCORE_HTTP_PORTS and uses the first value', () => {
        expect((detector as any).portFromLaunchConfig({ env: { ASPNETCORE_HTTP_PORTS: '5001;5002' } })).toBe(5001);
    });

    it('reads applicationUrl and prefers the HTTP entry', () => {
        const cfg = { applicationUrl: 'https://localhost:5001;http://localhost:5000' };
        expect((detector as any).portFromLaunchConfig(cfg)).toBe(5000);
    });

    it('returns null when nothing is configured', () => {
        expect((detector as any).portFromLaunchConfig({})).toBeNull();
    });
});

# EasyAuth Emulator VS Code Extension — Design Specification

## 1. Overview

### Purpose

Integrate EasyAuth Emulator into the local development IDE so that the emulator starts and stops automatically in sync with debug session start and end events, with automatic upstream port tracking.

### Scope

- New VS Code extension development
- Other IDE support is a future phase

### Packaging Policy

- **(b) bundles (c).** No separate installation of (c) is needed.
- **(c) can also be used standalone.** In non-IDE environments, install (c) only and configure it via the settings file.

---

## 2. Component Structure

```text
(a) Visual Studio Code
     ├── Debug session management
     └── Workspace files
          (launch.json / .env / launchSettings.json / application.properties, etc.)

          ↑ event reception / file reads / debug output event reception

(b) EasyAuth Emulator VS Code Extension          ← bundles (c)
     ├── Language / framework detection (cached per workspace)
     ├── Port detection logic
     ├── (c) state management
     ├── VS Code UI (notifications / settings / status bar)
     └── Process management
          (spawns and kills the easyauth-emulator binary as a child process via Node.js child_process)

          ↑ CLI invocation (easyauth-emulator ...)

(c) EasyAuth Emulator (core)
     ├── easyauth-emulator (binary, built with PyInstaller)
     ├── src/app.py (HTTP gateway, bundled in binary)
     └── oauth2-proxy (IdP auth proxy, auto-downloaded on first run)
```

### Design Principles

- **(b) depends heavily on (a).** Port detection and UI are entirely (b)'s responsibility.
- **(c) has no knowledge of port detection.** It simply starts the proxy with the values it receives.
- **(b) controls (c) via CLI only.** No control API is needed (not designed for persistent background use).
- **(c) operates standalone.** In non-IDE environments, edit the config file directly.
- **(b) runs as `extensionKind: ["workspace"]`.** VS Code automatically places (b) on the remote host in Remote - SSH / Remote - Tunnel environments, so port scanning, (c) startup, and file reads all work correctly on the remote host.

### Behavior in Remote Environments

When developing with Remote - SSH / Remote - Tunnel, the debugger, app, (b), and (c) all run on the remote host. Only the VS Code UI (status bar, notifications) is displayed on the local PC.

```text
Local PC                           Remote Host
────────────────────               ──────────────────────────────────
VS Code UI (display only)  ←─→   VS Code Extension Host
                                    ├─ (b) Extension (auto-deployed)
                                    ├─ (c) EasyAuth Emulator
                                    ├─ Debugger
                                    └─ App under development
```

**Additional requirements:** None. The easyauth-emulator binary is self-contained; Python is not required on the remote host.

---

## 3. Component Responsibilities

### (b) VS Code Extension

| Responsibility | Details |
| --- | --- |
| Debug detection | `vscode.debug.onDidStartDebugSession` / `onDidTerminateDebugSession` |
| Language / framework detection | Detected once per workspace and cached. Re-detection prompted on port detection failure (see §6) |
| Port acquisition | Read config files → parse stdout → port scan (see §6) |
| Process management | Start / stop / restart (c) binary via CLI (child process control via child_process) |
| State management | Monitor and hold (c) state (see §4) |
| UI | Status bar display, notifications, port confirmation dialog |
| Log forwarding | Display (c) stdout/stderr in the VS Code Output Channel |

### (c) EasyAuth Emulator Core

| Responsibility | Details |
| --- | --- |
| Proxy startup | Start the gateway and oauth2-proxy with the given APP_UPSTREAM |
| Config loading | config.toml (overridable via CLI options) |
| Standalone operation | Operates from config file alone, without an IDE |

---

## 4. (c) State Management

(b) monitors the (c) child process and holds the following states.

| State | Meaning | Transition Condition |
| --- | --- | --- |
| `stopped` | Not running | Initial state, or after a clean stop |
| `unconfigured` | Not configured | No IDP `clientId` set in VS Code settings |
| `missing_secret` | Secret not stored | `clientId` is set but no client secret is stored in SecretStorage |
| `missing_entra_issuer` | Entra Issuer URL missing | Entra `clientId` and secret are set but `oidcIssuerUrl` is empty (Entra only) |
| `starting` | Starting up | Immediately after launching `easyauth-emulator` |
| `running` | Running normally | `All processes started` detected in stdout |
| `error` | Abnormal exit | Process exited with non-zero code, or startup timed out |

**Timeout:** If `All processes started` is not detected within 30 seconds while in the `starting` state, the state transitions to `error`.

State is reflected in the status bar (see §10).

---

## 5. Lifecycle

### Session Binding Rules

(b) stores the ID of the debug session that started (c). When `onDidTerminateDebugSession` fires, (c) is stopped **only if the stored session ID matches**.

If multiple debug sessions start simultaneously, (c) is bound to the first session that triggered it. Subsequent new sessions are ignored (since (c) is designed to proxy a single upstream).

### Normal Flow

```text
[User] Start debugging in VS Code
    ↓
(b) Receives onDidStartDebugSession
    ↓
(b) Stores session ID
    ↓
(b) Port detection (see §6)
    ↓
(b) Starts easyauth-emulator (APP_UPSTREAM passed via env)  →  state: starting
    ↓
(b) Detects "All processes started" in stdout         →  state: running
    ↓                                (no detection within 30s → state: error)
[User] Development / testing
    ↓
[User] Stop debugging in VS Code
    ↓
(b) Receives onDidTerminateDebugSession (matches stored session ID)
    ↓
(b) Terminates (c) process                            →  state: stopped
```

### Port Change Flow (e.g., port conflict causes a port change)

```text
(b) Detects new port (differs from previous)
    ↓
(b) Terminates old (c) process                        →  state: stopped
    ↓
(b) Starts easyauth-emulator (new APP_UPSTREAM passed via env)  →  state: starting
```

### Abnormal Exit Flow

```text
(c) Process exits with non-zero code                  →  state: error
    ↓
(b) Shows notification in VS Code (with "Open Output" button)
```

### Child Process Cleanup

When (b) stops the emulator process, the entire process tree including `oauth2-proxy` children is terminated.

- **Windows:** `taskkill /F /T /PID <PID>` force-terminates the entire process tree
- **Linux:** `kill -TERM <PID>` sends a signal to the parent process

(b) only needs to act on the emulator's parent process; individual `oauth2-proxy` processes do not need to be managed separately.

---

## 6. Port Detection Specification

### Step 0: Language / Framework Detection (Cached)

Before port detection, the development language and framework are determined from files in the workspace.
**This runs once per workspace and the result is cached.**
Re-detection is prompted only when port detection fails and reaches the user confirmation UI.

| Detected File | Result |
| --- | --- |
| `*.csproj` / `launchSettings.json` | .NET |
| `pom.xml` / `build.gradle` | Java (Spring Boot) |
| `package.json` | Node.js |
| `requirements.txt` / `pyproject.toml` / `*.py` | Python |
| None of the above | Unknown (generic fallback) |

When multiple match, the `type` field in `launch.json` is used to disambiguate.

### Steps 1–6: Port Acquisition Priority

The following sources are tried in order; the first successful result is used.

| Priority | Source | Details |
| --- | --- | --- |
| 1 | Extension setting (manual) | `easyauth.upstreamPort` (null = auto) |
| 2 | `launch.json` | `env.PORT` / `env.ASPNETCORE_URLS` / `env.ASPNETCORE_HTTP_PORTS` / `applicationUrl` |
| 3 | Framework-specific config files | See table below |
| 4 | stdout parsing | Parse debug output events using framework-specific patterns (see table below) |
| 5 | Port scan | Fallback when all above fail |
| 6 | User confirmation UI | When scan is ambiguous, or scan base is unknown |

#### Selection Rule for Multiple URLs / Ports

When multiple URLs are listed (e.g., `ASPNETCORE_URLS`), one is selected using the following priority:

1. Prefer `http://` over `https://` (http is sufficient for local development)
2. If the same scheme appears multiple times, use the first one

Example: `https://localhost:7000;http://localhost:5000` → `5000` is used

#### Framework-specific Config Files (Priority 3)

| Framework | File | Key |
| --- | --- | --- |
| .NET | `launchSettings.json` | `applicationUrl` |
| Spring Boot | `application.properties` | `server.port` |
| Spring Boot | `application.yml` | `server.port` |
| Node.js / Python | `.env` | `PORT` |

#### stdout Parsing Patterns (Priority 4)

Used when the debug adapter exposes OutputEvents. Skipped if not available.

| Framework | Detection Pattern (regex) |
| --- | --- |
| .NET | `Now listening on: https?://[^:]+:(\d+)` |
| Spring Boot | `Tomcat started on port.? (\d+)` |
| Node.js / Express | `listening on.*port (\d+)` |
| Flask | `Running on http://[^:]+:(\d+)` |
| FastAPI / Uvicorn | `Uvicorn running on https?://[^:]+:(\d+)` |

#### Port Scan Specification (Priority 5)

- **Scan base:** `easyauth.portScanBase` (when `null`, scanning is skipped and priority 6 is tried immediately)
- **Scan range:** `easyauth.portScanMax` ports (default: 5) starting from the base
- **Method:** Attempt TCP connections to consecutive ports starting from the base; ports that respond are treated as candidates
- **False-positive mitigation:** Limiting the scan range to 5 ports reduces false positives. When multiple candidates remain, the user confirmation UI (priority 6) resolves the ambiguity.

#### User Confirmation UI (Priority 6)

| Situation | UI |
| --- | --- |
| One candidate | Applied automatically |
| Multiple candidates | User selects via `showQuickPick` |
| Zero candidates, or scan base unknown | User enters port manually via `showInputBox` |

---

## 7. Settings Specification

### (b) Extension Settings (VS Code settings.json)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `easyauth.autoStart` | boolean | `true` | Auto-start when a debug session begins |
| `easyauth.autoStop` | boolean | `true` | Auto-stop when the debug session ends |
| `easyauth.upstreamPort` | number \| null | `null` | Manual port override (null = auto-detect) |
| `easyauth.portScanMax` | number | `5` | Maximum number of ports to scan |
| `easyauth.portScanBase` | number \| null | `null` | Scan base port (when no hint is available) |
| `easyauth.verbose` | boolean | `false` | Print all resolved config values on startup (secrets masked) |

In addition to the above, many more settings exist for IDP configuration (`easyauth.entra.*`, `easyauth.google.*`, etc.), site settings (`easyauth.site.*`), TLS settings (`easyauth.tls.*`), and oauth2-proxy settings (`easyauth.oauth2proxy.*`). See the extension's Configuration Reference for details.

> **Binary path:** The extension uses the binary bundled in the VSIX. No custom path configuration is needed.
>
> **Config file:** The extension always passes `.vscode/easyauth.toml` to `--config`. If the file exists, it is loaded as the base configuration. If it does not exist, passing `--config` suppresses auto-discovery of a `config.toml` in the project root.

### (c) Config File

The config file format is `config.toml`.

| Condition | Behavior |
| --- | --- |
| `config.toml` exists | Loaded as the base configuration |
| Environment variables are set | Override values from `config.toml` (take precedence) |
| Neither present | Warning is output and startup continues (will fail immediately without IDP config) |

> (b) always passes `--config .vscode/easyauth.toml`. If the file exists it is loaded as base config; if not, auto-discovery of the project root `config.toml` is suppressed. IDP settings, site settings, and upstream settings are all passed as environment variables, overriding any config file values.

---

## 8. Secret Management

Client secrets and the cookie signing key are stored via the VS Code SecretStorage API in the platform's native secure store (OS keychain). This design avoids storing secrets as plaintext in `settings.json`.

### Storage Keys

| Key | Content | When Created |
| --- | --- | --- |
| `easyauth\|{workspaceUri}\|{idpKey}` | IdP client secret | User action via **Set Client Secret** command |
| `easyauth\|{workspaceUri}\|__cookieSecret__` | oauth2-proxy shared cookie signing key | Auto-generated on first startup (16 random bytes, Base64) |

`workspaceUri` is the workspace folder URI (e.g., `file:///c:/Users/user/myproject`). `idpKey` is the built-in IdP key (`entra` / `google` / `facebook` / `apple` / `github`) or a custom IdP's `custom:{name}`.

### Known Limitation

SecretStorage keys include the workspace folder URI. **If the project directory is deleted, moved, or renamed, stored secrets become orphaned.** Orphaned secrets cannot be deleted via the **Clear Client Secret** command and must be removed manually using the platform's keychain management tool.

---

## 9. CLI Interface Specification ((b) → (c) Control)

```text
easyauth-emulator [options]

Options:
  --app-upstream URL     Override APP_UPSTREAM (e.g. http://localhost:3000)
  --config PATH          Path to config file (default: ./config.toml)
  --verbose / -v         Print all resolved config values on startup (secrets masked)
```

> **(b) controls (c) via environment variables:** (b) passes `APP_UPSTREAM` and all IDP, site, and upstream settings as environment variables. Since (c) reads environment variables with `IDP_*`, `SITE_*`, `APP_*`, and `OAUTH2_PROXY_*` prefixes with higher priority than the config file, all settings from (b) are passed via environment variables. `--app-upstream` is an option for standalone use of (c) without the extension.

### Example

With `APP_UPSTREAM = "http://localhost:3000"` set in config.toml:

```sh
easyauth-emulator --app-upstream http://localhost:8081
```

→ Runs with `http://localhost:8081` as `APP_UPSTREAM`.

---

## 10. VS Code Status Bar

| State | Display | Click Behavior |
| --- | --- | --- |
| `stopped` | `$(shield) EasyAuth: stopped` | Detect port and start |
| `unconfigured` | `$(warning) EasyAuth: no config` | Open extension settings |
| `missing_secret` | `$(lock) EasyAuth: secret missing` (yellow background) | Prompt to enter client secret |
| `missing_entra_issuer` | `$(warning) EasyAuth: Entra issuer missing` (yellow background) | Open `easyauth.entra.oidcIssuerUrl` in workspace settings |
| `starting` | `$(sync~spin) EasyAuth: starting...` | Open Output Channel |
| `running` | `$(shield) EasyAuth: 8080:8081` (listen port : upstream port) | Open emulator in browser |
| `error` | `$(error) EasyAuth: error` | 1st click: open Output Channel / subsequent clicks: detect port and restart |

---

## 11. Command Palette Commands

| Command | Description |
| --- | --- |
| `EasyAuth Emulator: Start` | Detect port and start the emulator manually |
| `EasyAuth Emulator: Stop` | Stop the emulator |
| `EasyAuth Emulator: Restart` | Restart the emulator |
| `EasyAuth Emulator: Open Output` | Open the Output Channel |
| `EasyAuth Emulator: Open in Browser` | Open the emulator in a browser |
| `EasyAuth Emulator: Set Client Secret` | Save an IDP client secret to SecretStorage |
| `EasyAuth Emulator: Clear Client Secret` | Delete a stored client secret |

---

## 12. Future Extensions

| Target | Extension Reuse | Approach |
| --- | --- | --- |
| Cursor / Codex | Yes (VS Code-compatible API) | Reuse (b) as-is |
| Visual Studio | No (different extension model) | Implement separately as a C#/.NET VSIX. CLI control of (c) remains the same |
| Eclipse | No | Implement separately as a Java plugin. CLI control of (c) remains the same |
| Other IDEs | No | Standalone use via CLI or direct config file editing |

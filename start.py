#!/usr/bin/env python3
"""
Startup script for EasyAuth Emulator.

Starts app.py (HTTP gateway) and oauth2-proxy processes from pre-compiled
binaries based on config.toml configuration.

Usage:
    python start.py

Requirements:
    - Python 3.11+
    - bin/oauth2-proxy/oauth2-proxy.exe  (or oauth2-proxy on macOS/Linux)
    - config.toml  (copy from config.toml.example and fill in)

"""

import argparse
import base64
import json
import os
import platform
import re
import runpy
import secrets
import sys
import tarfile
import tempfile
import ssl
import time
import signal
import subprocess
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from urllib.error import HTTPError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

sys.stdout.reconfigure(line_buffering=True)

BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.resolve()))
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BUNDLE_DIR
SCRIPT_DIR = BUNDLE_DIR
BIN_DIR = BUNDLE_DIR / "bin"
SRC_DIR = BUNDLE_DIR / "src"
APP_PY = SRC_DIR / "app.py"
SAMPLE_APP_PY = SRC_DIR / "sample_app.py"
OAUTH2_PROXY_EXE = BIN_DIR / "oauth2-proxy" / ("oauth2-proxy.exe" if sys.platform == "win32" else "oauth2-proxy")

# ---------------------------------------------------------------------------
# Process registry (populated at runtime)
# ---------------------------------------------------------------------------

_processes: list[subprocess.Popen] = []

# ---------------------------------------------------------------------------
# SSL context (custom CA bundle for corporate proxies)
# ---------------------------------------------------------------------------

_ssl_context: ssl.SSLContext | None = None


def _configure_ssl(ca_bundle: str) -> None:
    global _ssl_context
    if ca_bundle:
        ca_path = Path(ca_bundle)
        if not ca_path.exists():
            print(f"[start] ERROR: SSL_CA_BUNDLE file not found: {ca_path}", file=sys.stderr)
            sys.exit(1)
        _ssl_context = ssl.create_default_context(cafile=str(ca_path))
        print(f"[start] Using custom CA bundle: {ca_path}")
    else:
        # Inject the native OS certificate store into Python's ssl module so that
        # PyInstaller frozen executables can verify HTTPS connections correctly on
        # Windows (CryptoAPI), macOS (Security framework), and Linux (system certs).
        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
        _ssl_context = None


_proc_stderr: dict[int, object] = {}  # pid → captured stderr (shown only on unexpected exit)

# ---------------------------------------------------------------------------
# Child-process lifetime management
# Windows  : Job Object with KILL_ON_JOB_CLOSE — children die when Python exits
# Linux    : prctl(PR_SET_PDEATHSIG, SIGTERM) set in each child via preexec_fn
# macOS    : signal handlers handle the normal case; no crash-case guarantee
# ---------------------------------------------------------------------------

_job_handle: int | None = None

_APP_CHILD_FLAG = "--run-app"
_SAMPLE_APP_CHILD_FLAG = "--run-sample-app"


def _pyinstaller_collect_hidden_imports() -> None:
    # PyInstaller analyzes import bytecode without executing this function.
    # Keeping the child apps importable here makes their transitive stdlib
    # dependencies part of the frozen bundle.
    from src import app as _embedded_app  # noqa: F401
    from src import sample_app as _embedded_sample_app  # noqa: F401


def _setup_job_object() -> None:
    """
    Windows only: create a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    When the Python process exits for any reason (Ctrl+C, terminal close,
    crash), the OS closes the job handle and automatically terminates all
    child processes assigned to it.
    """
    if sys.platform != "win32":
        return

    import ctypes

    global _job_handle

    kernel32 = ctypes.windll.kernel32

    class _BasicLimitInfo(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit",     ctypes.c_int64),
            ("LimitFlags",             ctypes.c_uint32),
            ("MinimumWorkingSetSize",  ctypes.c_size_t),
            ("MaximumWorkingSetSize",  ctypes.c_size_t),
            ("ActiveProcessLimit",     ctypes.c_uint32),
            ("Affinity",               ctypes.c_size_t),
            ("PriorityClass",          ctypes.c_uint32),
            ("SchedulingClass",        ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount",  ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount",   ctypes.c_uint64),
            ("WriteTransferCount",  ctypes.c_uint64),
            ("OtherTransferCount",  ctypes.c_uint64),
        ]

    class _ExtendedLimitInfo(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInfo),
            ("IoInfo",                _IoCounters),
            ("ProcessMemoryLimit",    ctypes.c_size_t),
            ("JobMemoryLimit",        ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed",     ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        print("[start] WARNING: CreateJobObjectW failed — child processes may not terminate automatically", file=sys.stderr)
        return

    info = _ExtendedLimitInfo()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    ok = kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        print("[start] WARNING: SetInformationJobObject failed — child processes may not terminate automatically", file=sys.stderr)
        kernel32.CloseHandle(job)
        return

    _job_handle = job


def _assign_to_job(proc: subprocess.Popen) -> None:
    """Windows only: add a child process to the Job Object."""
    if sys.platform != "win32" or _job_handle is None:
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    PROCESS_ALL_ACCESS = 0x1F0FFF
    handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
    if handle:
        kernel32.AssignProcessToJobObject(_job_handle, handle)
        kernel32.CloseHandle(handle)


def _child_preexec() -> None:
    """Linux only: called in child process before exec to request SIGTERM on parent death."""
    if sys.platform != "linux":
        return
    import ctypes
    import ctypes.util
    libc_name = ctypes.util.find_library("c")
    if libc_name:
        ctypes.CDLL(libc_name).prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG = 1


# preexec_fn is not supported on Windows; on Linux it sets PR_SET_PDEATHSIG.
# On macOS the function is a no-op (prctl is Linux-only).
_PREEXEC_FN = None if sys.platform == "win32" else _child_preexec


def _run_embedded_script(script_path: Path, argv0: str) -> None:
    sys.argv = [argv0, *sys.argv[2:]]
    runpy.run_path(str(script_path), run_name="__main__")


def _spawn_python_script(script_path: Path, child_flag: str,
                         extra_env: dict[str, str] | None = None,
                         extra_args: list[str] | None = None) -> subprocess.Popen:
    env = {**os.environ, **extra_env} if extra_env else None
    args = extra_args or []
    if getattr(sys, "frozen", False):
        return subprocess.Popen([sys.executable, child_flag, *args], preexec_fn=_PREEXEC_FN, env=env)
    return subprocess.Popen([sys.executable, str(script_path), *args], preexec_fn=_PREEXEC_FN, env=env)


# ---------------------------------------------------------------------------
# config.toml loading
# ---------------------------------------------------------------------------

def _load_config_file(path: Path) -> dict[str, str]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    result: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, bool):
            result[k] = "true" if v else "false"
        elif isinstance(v, list):
            result[k] = ",".join(str(item) for item in v)
        else:
            result[k] = str(v)
    return result


def _resolve_config_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path.cwd() / "config.toml"


def _get(env: dict, key: str, default: str = "") -> str:
    return env.get(key, default)



# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(f"[start] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


_SECRET_KEY_RE = re.compile(r"(SECRET|PASSWORD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL|PASSPHRASE|SALT|SIGNING_KEY)", re.IGNORECASE)


def _print_verbose_config(env: dict[str, str], config_file: Path) -> None:
    print(f"[start] Config file : {config_file}")
    print("[start] --- Resolved configuration (secrets masked) ---")
    for key in sorted(env):
        value = "***" if _SECRET_KEY_RE.search(key) else env[key]
        print(f"[start]   {key} = {value}")
    print("[start] ---")


def _ensure_cookie_secret(env: dict[str, str], config_file: Path) -> None:
    """Auto-generate OAUTH2_PROXY_COOKIE_SECRET if not set, and persist it to the config file."""
    if _get(env, "OAUTH2_PROXY_COOKIE_SECRET"):
        return
    secret = base64.b64encode(secrets.token_bytes(16)).decode()
    env["OAUTH2_PROXY_COOKIE_SECRET"] = secret
    content = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    new_content, n = re.subn(
        r'^(OAUTH2_PROXY_COOKIE_SECRET\s*=\s*).*$',
        f'OAUTH2_PROXY_COOKIE_SECRET = "{secret}"',
        content,
        flags=re.MULTILINE,
    )
    if n > 0:
        config_file.write_text(new_content, encoding="utf-8")
    else:
        with open(config_file, "a", encoding="utf-8") as f:
            f.write(f'\nOAUTH2_PROXY_COOKIE_SECRET = "{secret}"\n')
    print(f"[start] OAUTH2_PROXY_COOKIE_SECRET was not set — generated and saved to {config_file.name}")


# ---------------------------------------------------------------------------
# IDP processing
# ---------------------------------------------------------------------------

# (kind, default_issuer, default_auth_provider, default_claim, skip_claims_default)
_KIND_DEFAULTS: dict[str, tuple[str, str, str, str, bool]] = {
    "microsoft":    ("oidc", "https://login.microsoftonline.com/common/v2.0", "aad",      "preferred_username", True),
    "apple":        ("oidc", "https://appleid.apple.com",                     "apple",    "email",              False),
    "google":       ("oidc", "https://accounts.google.com",                   "google",   "email",              False),
    "openid-connect":("oidc","",                                               "oidc",     "sub",                False),
    "oidc":         ("oidc", "",                                               "oidc",     "sub",                False),
    "facebook":     ("facebook", "",                                           "facebook", "id",                 False),
    "github":       ("github",   "",                                           "github",   "login",              False),
}

_IDP_DEFAULT_KIND: dict[str, str] = {
    "entra":   "microsoft",
    "google":  "google",
    "apple":   "apple",
    "facebook":"facebook",
    "github":  "github",
}


def _process_idp(env: dict, idp: str, port: int,
                 base_site_url: str, whitelist_domain: str) -> dict:
    """Validate one IDP entry and return its oauth2-proxy launch args."""
    up_idp = idp.upper().replace("-", "_")
    pfx = f"IDP_{up_idp}"

    idp_kind = _get(env, f"{pfx}_KIND", "").lower() or _IDP_DEFAULT_KIND.get(idp, "openid-connect")

    if idp_kind not in _KIND_DEFAULTS:
        _die(f"Unsupported IDP kind for {idp}: {idp_kind}")

    oauth_provider_type, default_issuer, default_auth_provider, default_claim, default_skip = \
        _KIND_DEFAULTS[idp_kind]

    issuer_url    = _get(env, f"{pfx}_OIDC_ISSUER_URL", default_issuer)
    client_id     = _get(env, f"{pfx}_CLIENT_ID")
    client_secret = _get(env, f"{pfx}_CLIENT_SECRET")
    auth_provider = _get(env, f"{pfx}_AUTH_PROVIDER", default_auth_provider)
    user_id_claim = _get(env, f"{pfx}_AUTH_USER_ID_CLAIM", default_claim)
    scopes        = _get(env, f"{pfx}_SCOPES", "openid profile email")
    prompt        = _get(env, f"{pfx}_PROMPT", "")
    skip_raw      = _get(env, f"{pfx}_SKIP_CLAIMS_FROM_PROFILE_URL", "")
    skip_claims   = (skip_raw.lower() == "true") if skip_raw else default_skip

    if not client_id:
        _die(f"{pfx}_CLIENT_ID is required")
    if not client_secret:
        _die(f"{pfx}_CLIENT_SECRET is required")
    if oauth_provider_type == "oidc" and not issuer_url:
        _die(f"{pfx}_OIDC_ISSUER_URL is required for OIDC provider ({idp_kind})")

    cookie_secret = _get(env, "OAUTH2_PROXY_COOKIE_SECRET")
    cookie_secure = _get(env, "OAUTH2_PROXY_COOKIE_SECURE", "false")
    redirect_url  = f"{base_site_url}/oauth2/callback"
    cookie_name   = f"_oauth2_proxy_{idp}"

    args = [
        str(OAUTH2_PROXY_EXE),
        f"--provider={oauth_provider_type}",
        f"--client-id={client_id}",
        f"--client-secret={client_secret}",
        f"--cookie-secret={cookie_secret}",
        f"--cookie-name={cookie_name}",
        f"--http-address=0.0.0.0:{port}",
        f"--redirect-url={redirect_url}",
        "--email-domain=*",
        "--reverse-proxy=true",
        "--set-xauthrequest=true",
        "--pass-access-token=true",
        "--set-authorization-header=true",
        "--pass-authorization-header=true",
        "--skip-provider-button=true",
        f"--cookie-secure={cookie_secure}",
        f"--whitelist-domain={whitelist_domain}",
        "--upstream=static://202",
        f"--scope={scopes}",
    ]
    if skip_claims:
        args.append("--skip-claims-from-profile-url=true")
    if oauth_provider_type == "oidc":
        args.append(f"--oidc-issuer-url={issuer_url}")
        if prompt:
            args.append(f"--prompt={prompt}")

    return {
        "args": args,
        "port": port,
        "auth_provider": auth_provider,
        "user_id_claim": user_id_claim,
    }


# ---------------------------------------------------------------------------
# Shutdown / signal handling
# ---------------------------------------------------------------------------

def _shutdown(exit_code: int = 0) -> None:
    print("\n[start] Shutting down processes...")
    for proc in reversed(_processes):
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# oauth2-proxy auto-download
# ---------------------------------------------------------------------------

_GITHUB_LATEST = "https://api.github.com/repos/oauth2-proxy/oauth2-proxy/releases/latest"
_GITHUB_TAG    = "https://api.github.com/repos/oauth2-proxy/oauth2-proxy/releases/tags/{tag}"
_GITHUB_DOWNLOAD = "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/{tag}/{asset_name}"
_DEFAULT_OAUTH2_PROXY_VERSION = "v7.15.3"
_OAUTH2_PROXY_CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", str(RUNTIME_DIR))) / "easyauth-emulator" / "oauth2-proxy"

_OS_MAP: dict[str, str] = {
    "win32":  "windows",
    "darwin": "darwin",
    "linux":  "linux",
}
_ARCH_MAP: dict[str, str] = {
    "amd64":   "amd64",
    "x86_64":  "amd64",
    "arm64":   "arm64",
    "aarch64": "arm64",
    "armv7l":  "arm",
}


def _detect_platform() -> str:
    """Return 'os-arch' string for the current machine, or '' if unknown."""
    os_str   = _OS_MAP.get(sys.platform, "")
    arch_str = _ARCH_MAP.get(platform.machine().lower(), "")
    if os_str and arch_str:
        return f"{os_str}-{arch_str}"
    return ""


def _fetch_release(version: str = "") -> dict:
    """Fetch release metadata from GitHub. version='' → latest stable."""
    url = _GITHUB_TAG.format(tag=version) if version else _GITHUB_LATEST
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "easyauth-emulator"},
    )
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context) as resp:
        return json.loads(resp.read())


def _get_installed_version() -> str:
    """Return the version tag of the installed binary (e.g. 'v7.6.0'), or '' on failure."""
    try:
        r = subprocess.run(
            [str(OAUTH2_PROXY_EXE), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"v\d+\.\d+\.\d+", r.stdout + r.stderr)
        return m.group(0) if m else ""
    except Exception:
        return ""


def _cache_binary_path(platform_str: str, version: str) -> Path:
    filename = "oauth2-proxy.exe" if sys.platform == "win32" else "oauth2-proxy"
    return _OAUTH2_PROXY_CACHE_DIR / platform_str / version / filename


def _restore_cached_binary(platform_str: str, version: str) -> bool:
    cached = _cache_binary_path(platform_str, version)
    if not cached.exists():
        return False
    OAUTH2_PROXY_EXE.parent.mkdir(parents=True, exist_ok=True)
    OAUTH2_PROXY_EXE.write_bytes(cached.read_bytes())
    if sys.platform != "win32":
        OAUTH2_PROXY_EXE.chmod(OAUTH2_PROXY_EXE.stat().st_mode | 0o111)
    print(f"[start] Restored oauth2-proxy {version} from local cache")
    return True


def _save_binary_to_cache(platform_str: str, version: str) -> None:
    if not version or not OAUTH2_PROXY_EXE.exists():
        return
    cached = _cache_binary_path(platform_str, version)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(OAUTH2_PROXY_EXE.read_bytes())


def _download_file(url: str, destination: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "easyauth-emulator"})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context) as resp, open(destination, "wb") as dst:
        dst.write(resp.read())


def _install_from_release(platform_str: str, release: dict) -> None:
    """Extract and place the oauth2-proxy binary from a GitHub release dict."""
    tag    = release.get("tag_name", "")
    assets = release.get("assets", [])

    is_windows    = platform_str.startswith("windows")
    preferred_ext = ".zip" if is_windows else ".tar.gz"
    # Windows archives may ship the binary without .exe; accept both names.
    bin_names = ("oauth2-proxy.exe", "oauth2-proxy") if is_windows else ("oauth2-proxy",)

    _archive_exts = (".zip", ".tar.gz", ".tgz")
    archive_assets = [
        a for a in assets
        if platform_str in a["name"] and any(a["name"].endswith(ext) for ext in _archive_exts)
    ]
    asset = next((a for a in archive_assets if a["name"].endswith(preferred_ext)), None)
    if asset is None:
        asset = next(iter(archive_assets), None)
    if asset is None:
        _die(f"No asset found for platform '{platform_str}' in release {tag}")

    print(f"[start] Downloading {asset['name']} ({tag}) ...")
    OAUTH2_PROXY_EXE.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / asset["name"]
        _download_file(asset["browser_download_url"], archive_path)

        if asset["name"].endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                entry = next((n for n in zf.namelist() if Path(n).name in bin_names), None)
                if entry is None:
                    _die(f"oauth2-proxy binary not found inside the downloaded zip")
                with zf.open(entry) as src, open(OAUTH2_PROXY_EXE, "wb") as dst:
                    dst.write(src.read())
        else:
            with tarfile.open(archive_path) as tf:
                member = next((m for m in tf.getmembers() if Path(m.name).name in bin_names), None)
                if member is None:
                    _die(f"oauth2-proxy binary not found inside the downloaded archive")
                extracted = tf.extractfile(member)
                if extracted is None:
                    _die(f"Could not read oauth2-proxy binary from archive")
                with open(OAUTH2_PROXY_EXE, "wb") as dst:
                    dst.write(extracted.read())
            OAUTH2_PROXY_EXE.chmod(OAUTH2_PROXY_EXE.stat().st_mode | 0o111)

    _save_binary_to_cache(platform_str, tag)
    print(f"[start] oauth2-proxy {tag} installed at {OAUTH2_PROXY_EXE}")


def _install_from_known_tag(platform_str: str, tag: str) -> None:
    is_windows = platform_str.startswith("windows")
    asset_names = [
        f"oauth2-proxy-{tag}.{platform_str}.zip",
        f"oauth2-proxy-{tag}.{platform_str}.tar.gz",
        f"oauth2-proxy-{tag}.{platform_str}.tgz",
    ]
    bin_names = ("oauth2-proxy.exe", "oauth2-proxy") if is_windows else ("oauth2-proxy",)

    OAUTH2_PROXY_EXE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = None
        archive_name = ""
        for asset_name in asset_names:
            candidate = Path(tmp) / asset_name
            url = _GITHUB_DOWNLOAD.format(tag=tag, asset_name=asset_name)
            try:
                _download_file(url, candidate)
                archive_path = candidate
                archive_name = asset_name
                break
            except HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
        if archive_path is None:
            _die(f"No downloadable asset found for platform '{platform_str}' in release {tag}")

        print(f"[start] Downloading {archive_name} ({tag}) ...")
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                entry = next((n for n in zf.namelist() if Path(n).name in bin_names), None)
                if entry is None:
                    _die("oauth2-proxy binary not found inside the downloaded zip")
                with zf.open(entry) as src, open(OAUTH2_PROXY_EXE, "wb") as dst:
                    dst.write(src.read())
        else:
            with tarfile.open(archive_path) as tf:
                member = next((m for m in tf.getmembers() if Path(m.name).name in bin_names), None)
                if member is None:
                    _die("oauth2-proxy binary not found inside the downloaded archive")
                extracted = tf.extractfile(member)
                if extracted is None:
                    _die("Could not read oauth2-proxy binary from archive")
                with open(OAUTH2_PROXY_EXE, "wb") as dst:
                    dst.write(extracted.read())
            OAUTH2_PROXY_EXE.chmod(OAUTH2_PROXY_EXE.stat().st_mode | 0o111)

    _save_binary_to_cache(platform_str, tag)
    print(f"[start] oauth2-proxy {tag} installed at {OAUTH2_PROXY_EXE}")


def _manage_oauth2_proxy(platform_str: str, pinned: str, auto_update: bool) -> None:
    """
    Ensure oauth2-proxy is present and at the correct version.

    Decision table:
      binary absent             → download (pinned or latest)
      pinned version set        → update when installed != pinned
      no pin, auto_update=true  → update when installed != latest
      no pin, auto_update=false → notify when installed != latest (no update)
    """
    _PLATFORM_ERR = (
        "Cannot auto-detect platform for oauth2-proxy download. "
        "Set OAUTH2_PROXY_PLATFORM in config.toml (e.g. OAUTH2_PROXY_PLATFORM = \"linux-amd64\")"
    )
    fallback_version = pinned or _DEFAULT_OAUTH2_PROXY_VERSION

    # Binary absent: always download
    if not OAUTH2_PROXY_EXE.exists():
        if not platform_str:
            _die(_PLATFORM_ERR)
        if fallback_version and _restore_cached_binary(platform_str, fallback_version):
            return
        label = pinned if pinned else "latest"
        print(f"[start] oauth2-proxy not found. Downloading {label}...")
        try:
            _install_from_release(platform_str, _fetch_release(pinned))
        except HTTPError as exc:
            if not pinned and exc.code == 403:
                print(
                    f"[start] GitHub API rate limit hit while resolving latest version; falling back to {_DEFAULT_OAUTH2_PROXY_VERSION}",
                    file=sys.stderr,
                )
                _install_from_known_tag(platform_str, _DEFAULT_OAUTH2_PROXY_VERSION)
                return
            raise
        return

    # Pinned version: enforce exact match
    if pinned:
        installed = _get_installed_version()
        if installed == pinned:
            print(f"[start] oauth2-proxy {installed} (pinned)")
            return
        if not platform_str:
            _die(_PLATFORM_ERR)
        print(f"[start] oauth2-proxy version mismatch ({installed or '?'} → {pinned}), updating...")
        _install_from_release(platform_str, _fetch_release(pinned))
        return

    # No pin: check latest version; skip and continue if network is unavailable
    print("[start] Checking for oauth2-proxy updates...")
    try:
        release = _fetch_release()
    except Exception:
        print("[start] Skipped update check (network unavailable)")
        return

    latest    = release.get("tag_name", "")
    installed = _get_installed_version()

    if not latest or installed == latest:
        print(f"[start] oauth2-proxy {installed} (up to date)")
        return

    if auto_update:
        if not platform_str:
            _die(_PLATFORM_ERR)
        print(f"[start] Updating oauth2-proxy: {installed or '?'} → {latest}")
        _install_from_release(platform_str, release)
    else:
        print(
            f"[start] oauth2-proxy {installed} is installed "
            f"(latest: {latest}). "
            "Set OAUTH2_PROXY_AUTO_UPDATE = true in config.toml to update automatically.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ---- Parse CLI arguments ---------------------------------------------
    parser = argparse.ArgumentParser(description="EasyAuth Emulator")
    parser.add_argument("--upstream-port", type=int, default=None,
                        metavar="PORT", help="Override APP_UPSTREAM port")
    parser.add_argument("--config", type=str, default=None,
                        metavar="PATH", help="Path to config.json or config.toml")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print all resolved configuration values (secrets masked)")
    args = parser.parse_args()

    # ---- Load config file ------------------------------------------------
    env: dict[str, str] = {}
    config_file = _resolve_config_file(args.config)
    if config_file.exists():
        env = _load_config_file(config_file)

    # ---- Apply environment variable overrides (e.g. from VS Code extension) --
    for key, val in os.environ.items():
        if any(key.startswith(p) for p in ('IDP_', 'SITE_', 'APP_', 'OAUTH2_PROXY_', 'SAMPLE_APP_')):
            env[key] = val

    if not config_file.exists() and not any(k.startswith('IDP_') for k in env):
        print("[start] WARNING: config file not found and no IDP environment variables set", file=sys.stderr)

    # ---- Apply --upstream-port override ----------------------------------
    if args.upstream_port is not None:
        upstream = _get(env, "APP_UPSTREAM", f"http://localhost:{args.upstream_port}")
        new_upstream = re.sub(r":\d+(?=/|$)", f":{args.upstream_port}", upstream)
        env["APP_UPSTREAM"] = new_upstream

    _ensure_cookie_secret(env, config_file)

    # ---- Verbose config dump -----------------------------------------------
    verbose = args.verbose or _get(env, "VERBOSE", "false").lower() == "true"
    if verbose:
        _print_verbose_config(env, config_file)

    # ---- SSL: custom CA bundle for corporate proxies -------------------------
    _configure_ssl(_get(env, "SSL_CA_BUNDLE") or os.environ.get("SSL_CA_BUNDLE", ""))

    # ---- oauth2-proxy: version check / download / update --------------------
    platform_str = _get(env, "OAUTH2_PROXY_PLATFORM") or _detect_platform()
    pinned       = _get(env, "OAUTH2_PROXY_VERSION", "").strip()
    auto_update  = _get(env, "OAUTH2_PROXY_AUTO_UPDATE", "false").lower() == "true"
    try:
        _manage_oauth2_proxy(platform_str, pinned, auto_update)
    except Exception as exc:
        _die(f"Failed to manage oauth2-proxy: {exc}")

    # ---- Validate app.py --------------------------------------------------
    if not APP_PY.exists():
        _die(f"app.py not found: {APP_PY}")

    # ---- Job Object: auto-kill children on any exit -----------------------
    _setup_job_object()

    # ---- Read / validate configuration ------------------------------------
    site_url         = _get(env, "SITE_URL", "http://localhost").rstrip("/")
    site_port        = _get(env, "SITE_PORT", "8080")
    sample_app_port  = _get(env, "SAMPLE_APP_PORT", "8081")
    app_upstream     = _get(env, "APP_UPSTREAM", f"http://localhost:{sample_app_port}")
    idp_list_raw = _get(env, "IDP_LIST", "entra")
    idp_list     = [x.strip().lower() for x in idp_list_raw.split(",") if x.strip()]

    if not idp_list:
        _die("IDP_LIST must include at least one IDP")

    if site_url.startswith("https://"):
        default_port, site_host = "443", site_url[len("https://"):]
    elif site_url.startswith("http://"):
        default_port, site_host = "80", site_url[len("http://"):]
    else:
        _die("SITE_URL must start with http:// or https://")

    site_host = site_host.split("/")[0]
    if site_port == default_port:
        base_site_url     = site_url
        default_whitelist = site_host
    else:
        base_site_url     = f"{site_url}:{site_port}"
        default_whitelist = f"{site_host}:{site_port}"

    whitelist_domain = _get(env, "OAUTH2_PROXY_WHITELIST_DOMAIN", default_whitelist)

    # ---- Process each IDP -------------------------------------------------
    idp_configs: list[tuple[str, dict]] = []

    try:
        PORT_BASE = int(_get(env, "OAUTH2_PROXY_PORT_BASE", "4180"))
    except ValueError:
        _die("OAUTH2_PROXY_PORT_BASE must be an integer")

    for i, idp in enumerate(idp_list):
        if not re.fullmatch(r"[a-z0-9_\-]+", idp):
            _die(f"Invalid IDP name in IDP_LIST: {idp!r}")

        port = PORT_BASE + i
        cfg  = _process_idp(env, idp, port, base_site_url, whitelist_domain)
        idp_configs.append((idp, cfg))

    sample_app_enabled   = _get(env, "SAMPLE_APP_ENABLED",           "false").lower() == "true"
    proxy_standard_log   = _get(env, "OAUTH2_PROXY_STANDARD_LOGGING", "false").lower() == "true"
    proxy_auth_log       = _get(env, "OAUTH2_PROXY_AUTH_LOGGING",     "false").lower() == "true"
    proxy_request_log    = _get(env, "OAUTH2_PROXY_REQUEST_LOGGING",  "false").lower() == "true"
    proxy_any_log        = proxy_standard_log or proxy_auth_log or proxy_request_log
    # Pass explicit flags only when at least one is true.
    # Passing all three as false triggers oauth2-proxy's "Logging disabled" state,
    # which also suppresses error messages — so we avoid that by not passing them.
    proxy_log_args = (
        [
            f"--standard-logging={'true' if proxy_standard_log else 'false'}",
            f"--auth-logging={'true' if proxy_auth_log else 'false'}",
            f"--request-logging={'true' if proxy_request_log else 'false'}",
        ]
        if proxy_any_log else []
    )
    proxy_show_debug = _get(env, "OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR", "false").lower() == "true"
    if proxy_show_debug:
        proxy_log_args.append("--show-debug-on-error")

    # ---- Summary ----------------------------------------------------------
    print(f"[start] Site URL    : {base_site_url}")
    print(f"[start] App upstream: {app_upstream}")
    print(f"[start] IDPs        : {', '.join(idp_list)}")
    if SAMPLE_APP_PY.exists() and sample_app_enabled:
        print(f"[start] Sample app  : http://localhost:{sample_app_port} (internal)")

    # ---- Signal handlers --------------------------------------------------
    signal.signal(signal.SIGINT,  lambda s, f: _shutdown())
    signal.signal(signal.SIGTERM, lambda s, f: _shutdown())

    # ---- Launch oauth2-proxy instances ------------------------------------
    for idp, cfg in idp_configs:
        print(f"[start] Starting oauth2-proxy for '{idp}' on port {cfg['port']}")
        if proxy_any_log:
            # Let all output flow to the terminal directly.
            proc = subprocess.Popen(cfg["args"] + proxy_log_args, preexec_fn=_PREEXEC_FN)
            stderr_cap = None
        else:
            # Capture stdout+stderr silently; displayed only on unexpected exit.
            # No log-control flags are passed so oauth2-proxy keeps its defaults
            # (all logging on), ensuring error messages are not suppressed.
            stderr_cap = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                cfg["args"],
                stdout=stderr_cap,
                stderr=subprocess.STDOUT,
                preexec_fn=_PREEXEC_FN,
            )
        _proc_stderr[proc.pid] = stderr_cap
        _processes.append(proc)
        _assign_to_job(proc)

    # ---- Launch app.py (pass overrides via env) ---------------------------
    child_env: dict[str, str] = {}
    if args.upstream_port is not None:
        child_env["APP_UPSTREAM"] = _get(env, "APP_UPSTREAM", "")
    app_extra_args: list[str] = []
    if args.config is not None:
        app_extra_args = ["--config", str(config_file)]
    print(f"[start] Starting app.py on port {site_port}")
    app_proc = _spawn_python_script(APP_PY, _APP_CHILD_FLAG, child_env or None, app_extra_args or None)
    _processes.append(app_proc)
    _assign_to_job(app_proc)

    # ---- Launch sample_app.py (verification app) --------------------------
    if SAMPLE_APP_PY.exists() and sample_app_enabled:
        print(f"[start] Starting sample_app.py on port {sample_app_port}")
        sample_proc = _spawn_python_script(SAMPLE_APP_PY, _SAMPLE_APP_CHILD_FLAG)
        _processes.append(sample_proc)
        _assign_to_job(sample_proc)

    # ---- Monitor processes ------------------------------------------------
    print("[start] All processes started. Press Ctrl+C to stop.")
    try:
        while True:
            for proc in _processes:
                ret = proc.poll()
                if ret is not None:
                    name = Path(proc.args[0]).name if proc.args else "process"
                    print(f"[start] {name} exited unexpectedly (code {ret})", file=sys.stderr)
                    cap = _proc_stderr.get(proc.pid)
                    if cap is not None:
                        cap.seek(0)
                        output = cap.read().decode(errors="replace").strip()
                        if output:
                            for line in output.splitlines():
                                print(f"[start]   {line}", file=sys.stderr)
                    _shutdown(1)
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == _APP_CHILD_FLAG:
        _run_embedded_script(APP_PY, str(APP_PY))
    elif len(sys.argv) > 1 and sys.argv[1] == _SAMPLE_APP_CHILD_FLAG:
        _run_embedded_script(SAMPLE_APP_PY, str(SAMPLE_APP_PY))
    else:
        main()

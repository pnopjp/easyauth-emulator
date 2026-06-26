#!/usr/bin/env python3
"""
Local packaging script for easyauth-emulator.
Runs PyInstaller then creates a distributable zip (Windows) or tar.gz (Linux/macOS).
With --vsix, also builds the VS Code extension VSIX.

Usage:
    python scripts/package.py [--skip-build] [--vsix]

Options:
    --skip-build    Skip PyInstaller; repackage the existing dist/ output.
    --vsix          Also build the VS Code extension VSIX after packaging the binary.
"""
import argparse
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "dist" / "easyauth-emulator"
VSCODE_DIR = ROOT / "vscode-extension"

_OS_MAP = {
    "win32":  "windows",
    "darwin": "darwin",
    "linux":  "linux",
}
_ARCH_MAP = {
    "amd64":   "amd64",
    "x86_64":  "amd64",
    "arm64":   "arm64",
    "aarch64": "arm64",
    "armv7l":  "arm",
}
_VSCODE_TARGET_MAP = {
    "windows-amd64": "win32-x64",
    "darwin-amd64":  "darwin-x64",
    "darwin-arm64":  "darwin-arm64",
    "linux-amd64":   "linux-x64",
    "linux-arm64":   "linux-arm64",
}


def detect_platform() -> str:
    os_str   = _OS_MAP.get(sys.platform, sys.platform)
    arch_str = _ARCH_MAP.get(platform.machine().lower(), platform.machine().lower())
    return f"{os_str}-{arch_str}"


def run_pyinstaller() -> None:
    print("[package] Running PyInstaller ...")
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "easyauth-emulator.spec"],
        cwd=ROOT,
    )
    if r.returncode != 0:
        sys.exit(r.returncode)
    print("[package] Build complete.")


def create_archive(platform_str: str) -> Path:
    out_stem = ROOT / "dist" / f"easyauth-emulator-{platform_str}"
    license_src = ROOT / "LICENSE"
    third_party_src = ROOT / "THIRD_PARTY_LICENSES"

    if platform_str.startswith("windows"):
        archive = out_stem.with_suffix(".zip")
        print(f"[package] Creating {archive.name} ...")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for f in DIST_DIR.rglob("*"):
                if f.is_file():
                    zf.write(f, Path("easyauth-emulator") / f.relative_to(DIST_DIR))
            zf.write(license_src, Path("easyauth-emulator") / "LICENSE")
            zf.write(third_party_src, Path("easyauth-emulator") / "THIRD_PARTY_LICENSES")
    else:
        archive = out_stem.with_suffix(".tar.gz")
        print(f"[package] Creating {archive.name} ...")
        with tarfile.open(archive, "w:gz", compresslevel=9) as tf:
            tf.add(DIST_DIR, arcname="easyauth-emulator")
            tf.add(license_src, arcname="easyauth-emulator/LICENSE")
            tf.add(third_party_src, arcname="easyauth-emulator/THIRD_PARTY_LICENSES")

    size_mb = archive.stat().st_size / 1_048_576
    print(f"[package] {archive.name}  ({size_mb:.1f} MB)")
    return archive


def build_vsix(platform_str: str) -> None:
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    vscode_target = _VSCODE_TARGET_MAP.get(platform_str)
    bin_dir = VSCODE_DIR / "bin" / "easyauth-emulator"
    license_dst = VSCODE_DIR / "LICENSE"
    third_party_dst = VSCODE_DIR / "THIRD_PARTY_LICENSES"

    print("[vsix] Copying binary into vscode-extension/bin/ ...")
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    shutil.copytree(DIST_DIR, bin_dir)
    shutil.copy2(ROOT / "LICENSE", license_dst)
    shutil.copy2(ROOT / "THIRD_PARTY_LICENSES", third_party_dst)

    try:
        node_modules = VSCODE_DIR / "node_modules"
        if node_modules.exists():
            shutil.rmtree(node_modules)
        print("[vsix] Installing npm dependencies ...")
        r = subprocess.run([npm, "ci"], cwd=VSCODE_DIR)
        if r.returncode != 0:
            sys.exit(r.returncode)

        print("[vsix] Packaging VSIX ...")
        cmd = [npx, "vsce", "package"]
        if vscode_target:
            cmd += ["--target", vscode_target]
        r = subprocess.run(cmd, cwd=VSCODE_DIR)
        if r.returncode != 0:
            sys.exit(r.returncode)
    finally:
        shutil.rmtree(bin_dir, ignore_errors=True)
        license_dst.unlink(missing_ok=True)
        third_party_dst.unlink(missing_ok=True)

    print("[vsix] VSIX build complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-build", action="store_true", help="Skip PyInstaller, repackage existing dist/")
    parser.add_argument("--vsix", action="store_true", help="Also build the VS Code extension VSIX")
    args = parser.parse_args()

    platform_str = detect_platform()
    print(f"[package] Platform: {platform_str}")

    if not args.skip_build:
        run_pyinstaller()

    if not DIST_DIR.exists():
        print(f"[package] ERROR: {DIST_DIR} not found. Run without --skip-build first.", file=sys.stderr)
        sys.exit(1)

    create_archive(platform_str)

    if args.vsix:
        build_vsix(platform_str)

    print("[package] Done.")


if __name__ == "__main__":
    main()

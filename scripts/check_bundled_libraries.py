#!/usr/bin/env python3
"""
Verify every shared library PyInstaller bundled into the Linux release
binary's _internal/ directory is accounted for in scripts/gen_licenses.py's
SYSTEM_LIBRARY_METADATA. Catches the case where a future dependency change
causes PyInstaller to start bundling some new native library that nobody
has added a license entry for.

This can only run after an actual Linux build (needs
dist/easyauth-emulator/_internal/ to exist), so it's a separate step in
release.yml's build job, not part of the Docker-free gen_licenses.py --check.

Usage:
    python scripts/check_bundled_libraries.py dist/easyauth-emulator
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES_FILE = ROOT / "scripts" / "manylinux_images.json"


def _known_prefixes() -> dict:
    """Category key (matches gen_licenses.py's SYSTEM_LIBRARY_METADATA) ->
    list of shared-library filename prefixes that belong to it."""
    python_version = json.loads(IMAGES_FILE.read_text(encoding="utf-8"))["python_version"]
    return {
        "python": [f"libpython{python_version}.so"],
        # openssl-libs also ships ".libssl.so.<ver>.hmac" / ".libcrypto.so.<ver>.hmac"
        # sidecar files (FIPS integrity-check checksums, not separate software) --
        # PyInstaller bundles these alongside the .so files they check.
        "openssl": ["libssl.so", "libcrypto.so", ".libssl.so", ".libcrypto.so"],
        "zlib": ["libz.so"],
        "xz": ["liblzma.so"],
        "libffi": ["libffi.so"],
        "bzip2": ["libbz2.so"],
        "mpdecimal": ["libmpdec.so"],
    }


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_bundled_libraries.py <path to dist/easyauth-emulator>", file=sys.stderr)
        sys.exit(2)

    dist_dir = Path(sys.argv[1])
    internal_dir = dist_dir / "_internal"
    if not internal_dir.is_dir():
        print(f"[check-bundled] ERROR: {internal_dir} not found.", file=sys.stderr)
        sys.exit(1)

    prefixes = _known_prefixes()

    so_files = sorted(f.name for f in internal_dir.glob("*.so*") if f.is_file())

    print(f"[check-bundled] Found {len(so_files)} shared libraries in {internal_dir}:")
    unaccounted = []
    for name in so_files:
        matched = next((key for key, plist in prefixes.items() if any(name.startswith(p) for p in plist)), None)
        print(f"  {name}  ->  {matched or 'UNACCOUNTED FOR'}")
        if matched is None:
            unaccounted.append(name)

    if unaccounted:
        print(file=sys.stderr)
        print("[check-bundled] ERROR: the following bundled libraries have no license entry", file=sys.stderr)
        print("in scripts/gen_licenses.py's SYSTEM_LIBRARY_METADATA:", file=sys.stderr)
        for name in unaccounted:
            print(f"  {name}", file=sys.stderr)
        print(file=sys.stderr)
        print("Add a corresponding entry there (and an RPM package + license path in", file=sys.stderr)
        print("scripts/refresh_manylinux_licenses.py's _packages()) before releasing.", file=sys.stderr)
        sys.exit(1)

    print("[check-bundled] All bundled libraries are accounted for.")


if __name__ == "__main__":
    main()

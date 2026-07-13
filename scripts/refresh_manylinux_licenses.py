#!/usr/bin/env python3
"""
Re-derive scripts/manylinux_licenses.json from the actual RPM packages inside
the pinned manylinux build image (scripts/manylinux_images.json).

Run this after bumping that image's digest or python_version, then regenerate
THIRD_PARTY_LICENSES (python scripts/gen_licenses.py) and commit both files.
.github/workflows/manylinux-license-refresh.yml does this automatically
whenever manylinux_images.json changes.

Only inspects the x86_64 image -- AlmaLinux RPM package versions are
identical across architectures for a given point release, so this is
sufficient for both the linux-amd64 and linux-arm64 release binaries.

Requires Docker.

Usage:
    python scripts/refresh_manylinux_licenses.py           # write manylinux_licenses.json
    python scripts/refresh_manylinux_licenses.py --check   # exit(1) if file is out of date
"""
import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES_FILE = ROOT / "scripts" / "manylinux_images.json"
OUTPUT = ROOT / "scripts" / "manylinux_licenses.json"

VERSION_MARKER = "###VERSION"
LICENSE_MARKER = "###LICENSE"
END_MARKER = "###END"


def _packages(python_version: str) -> dict:
    """Stable key -> (rpm package name, license file path inside the image).

    Keys are stable identifiers, not tied to any version, so gen_licenses.py
    never needs to change just because python_version (or any package's own
    version) is bumped -- only the values here (and the pinned image) do.
    """
    return {
        "python": (f"python{python_version}-libs", f"/usr/lib64/python{python_version}/LICENSE.txt"),
        "openssl": ("openssl-libs", "/usr/share/licenses/openssl/LICENSE"),
        "zlib": ("zlib", "/usr/share/licenses/zlib/README"),
        "xz": ("xz-libs", "/usr/share/doc/xz/COPYING"),
        "libffi": ("libffi", "/usr/share/licenses/libffi/LICENSE"),
        "bzip2": ("bzip2-libs", "/usr/share/licenses/bzip2-libs/LICENSE"),
        "mpdecimal": ("mpdecimal", "/usr/share/licenses/mpdecimal/LICENSE.txt"),
    }


def _build_inner_script(python_version: str, packages: dict) -> str:
    lines = [
        "set -e",
        f"dnf install -y python{python_version} python{python_version}-devel mpdecimal >/dev/null 2>&1",
    ]
    for key, (rpm_package, _path) in packages.items():
        lines.append(f"echo '{VERSION_MARKER} {key}' $(rpm -q --qf '%{{VERSION}}' {rpm_package})")
    for key, (_rpm_package, path) in packages.items():
        lines.append(f"echo '{LICENSE_MARKER} {key}'")
        lines.append(f"cat '{path}'")
    lines.append(f"echo '{END_MARKER}'")
    return "\n".join(lines)


def _parse_output(output: str, packages: dict) -> dict:
    versions = {}
    license_texts = {}
    current_key = None
    current_license_lines = []

    def flush_license():
        if current_key is not None:
            license_texts[current_key] = "\n".join(current_license_lines).strip("\n")

    for line in output.splitlines():
        if line.startswith(VERSION_MARKER):
            _, key, version = line.split(" ", 2)
            versions[key] = version
        elif line.startswith(LICENSE_MARKER):
            flush_license()
            current_key = line.split(" ", 1)[1]
            current_license_lines = []
        elif line.startswith(END_MARKER):
            flush_license()
            current_key = None
        elif current_key is not None:
            current_license_lines.append(line)

    result = {}
    for key, (rpm_package, _path) in packages.items():
        if key not in versions or key not in license_texts:
            print(f"[refresh] ERROR: missing data for package '{key}' ({rpm_package})", file=sys.stderr)
            sys.exit(1)
        result[key] = {
            "rpm_package": rpm_package,
            "version": versions[key],
            "license_text": license_texts[key],
        }
    return result


def _derive() -> dict:
    images = json.loads(IMAGES_FILE.read_text(encoding="utf-8"))
    image = images["x86_64"]
    python_version = images["python_version"]
    packages = _packages(python_version)

    print(f"[refresh] Running rpm queries inside {image} ...", file=sys.stderr)
    proc = subprocess.run(
        ["docker", "run", "--rm", image, "bash", "-c", _build_inner_script(python_version, packages)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        sys.exit(proc.returncode)

    data = _parse_output(proc.stdout, packages)
    data["_source_image"] = image
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Exit with error if manylinux_licenses.json is out of date")
    args = parser.parse_args()

    data = _derive()
    content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    if args.check:
        if not OUTPUT.exists():
            print(f"[refresh] ERROR: {OUTPUT.name} does not exist. Run without --check to generate it.", file=sys.stderr)
            sys.exit(1)
        current = OUTPUT.read_text(encoding="utf-8")
        if current != content:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"{OUTPUT.name} (current)",
                tofile=f"{OUTPUT.name} (expected)",
            )
            print(f"[refresh] ERROR: {OUTPUT.name} is out of date. Run `python scripts/refresh_manylinux_licenses.py` and commit the result.", file=sys.stderr)
            print("[refresh] Diff:", file=sys.stderr)
            sys.stderr.writelines(diff)
            sys.exit(1)
        print(f"[refresh] {OUTPUT.name} is up to date.")
    else:
        OUTPUT.write_text(content, encoding="utf-8", newline="\n")
        print(f"[refresh] Written: {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()

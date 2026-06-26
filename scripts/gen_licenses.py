#!/usr/bin/env python3
"""
Generate THIRD_PARTY_LICENSES from installed package metadata.
Run after updating Python dependencies to keep the file current.

Usage:
    python scripts/gen_licenses.py           # generate THIRD_PARTY_LICENSES
    python scripts/gen_licenses.py --check   # exit(1) if file is out of date
"""
import argparse
import importlib.metadata
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
OUTPUT = ROOT / "THIRD_PARTY_LICENSES"

OAUTH2_PROXY_ENTRY = {
    "name": "oauth2-proxy",
    "license_id": "MIT",
    "homepage": "https://github.com/oauth2-proxy/oauth2-proxy",
    "note": "Not bundled. Downloaded from GitHub Releases at runtime.",
    "license_text": """\
MIT License

Copyright (c) 2018 OAuth2 Proxy Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.""",
}

HEADER = """\
Third-Party Licenses
====================

This file lists the open-source components used by easyauth-emulator.
Each component retains its original license.

The easyauth-emulator project itself is licensed under the Apache License 2.0
(see LICENSE).

"""

SEP = "=" * 72


def _read_requirements() -> list[str]:
    """Read package names from requirements.txt (strips version specifiers)."""
    packages = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifier (==, >=, <=, ~=, !=, >, <)
        for op in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            if op in line:
                line = line[: line.index(op)].strip()
                break
        packages.append(line)
    return packages


def _find_license_text(pkg: str) -> str:
    files = importlib.metadata.files(pkg)
    if not files:
        return ""
    candidates = [
        f for f in files
        if "dist-info" in str(f)
        and any(
            seg in Path(str(f)).name.upper()
            for seg in ("LICENSE", "LICENCE", "COPYING", "COPYRIGHT")
        )
    ]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
            if text.strip():
                return text.strip()
        except Exception:
            continue
    return ""


def _license_id(meta: importlib.metadata.PackageMetadata) -> str:
    value = meta.get("License-Expression") or meta.get("License") or ""
    if value:
        return value
    classifiers = meta.get_all("Classifier") or []
    ids = [
        c.split(" :: ")[-1]
        for c in classifiers
        if c.startswith("License ::")
    ]
    return ", ".join(ids)


def _python_package_section(pkg: str) -> str:
    meta = importlib.metadata.metadata(pkg)
    name = meta["Name"]
    version = meta["Version"]
    homepage = meta.get("Home-page") or ""
    license_id = _license_id(meta)
    license_text = _find_license_text(pkg)

    lines = [SEP, f"{name} {version}"]
    if license_id:
        lines.append(f"License: {license_id}")
    if homepage:
        lines.append(f"Homepage: {homepage}")
    lines += [SEP, "", license_text if license_text else "(License text not available in package metadata)"]
    return "\n".join(lines)


def _oauth2_proxy_section() -> str:
    e = OAUTH2_PROXY_ENTRY
    lines = [
        SEP,
        e["name"],
        f"License: {e['license_id']}",
        f"Homepage: {e['homepage']}",
        f"Note: {e['note']}",
        SEP,
        "",
        e["license_text"],
    ]
    return "\n".join(lines)


def _generate() -> str:
    packages = _read_requirements()
    sections = [_python_package_section(pkg) for pkg in packages]
    sections.append(_oauth2_proxy_section())
    return HEADER + "\n\n\n".join(sections) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Exit with error if THIRD_PARTY_LICENSES is out of date")
    args = parser.parse_args()

    content = _generate()

    if args.check:
        if not OUTPUT.exists():
            print(f"[gen_licenses] ERROR: {OUTPUT.name} does not exist. Run without --check to generate it.", file=sys.stderr)
            sys.exit(1)
        current = OUTPUT.read_text(encoding="utf-8")
        if current != content:
            print(f"[gen_licenses] ERROR: {OUTPUT.name} is out of date. Run `python scripts/gen_licenses.py` and commit the result.", file=sys.stderr)
            sys.exit(1)
        print(f"[gen_licenses] {OUTPUT.name} is up to date.")
    else:
        OUTPUT.write_text(content, encoding="utf-8")
        print(f"[gen_licenses] Written: {OUTPUT}")


if __name__ == "__main__":
    main()

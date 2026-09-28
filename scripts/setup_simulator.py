#!/usr/bin/env python3
"""Install a checksum-pinned OSS CAD Suite locally, without a package manager.

Run from any directory with Python 3.9+: python3 scripts/setup_simulator.py
The archive is official YosysHQ release 2026-09-27. All hashes are recorded from
GitHub's release asset digests, not calculated from an unverified download.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

RELEASE = "2026-09-27"
DATE = "20260927"
HASHES = {
    "darwin-arm64": "0df7ff004bb038f5aaef70959da0ba9c23c19472f5887698673d36f4b9528455",
    "darwin-x64": "52211c41af2473f7212de20b1d5a356500bd9d35b16339f4f58b60f381f1de82",
    "linux-arm64": "5f98759bb349995168a7247c8b0fbd4a3e80def99b514c58b209717b7893a4ef",
    "linux-x64": "8af3500957e8a304a9bb67ecbcd1e9a7a8bc6ae0813075cc812bfac6143aed86",
}
ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-dir", type=Path, default=ROOT / ".tools")
    args = parser.parse_args()
    machine = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
    host = f"{platform.system().lower()}-{machine}"
    if host not in HASHES:
        parser.error(f"unsupported platform {host}; supported: {', '.join(HASHES)}")
    tools = args.tools_dir.resolve()
    tools.mkdir(parents=True, exist_ok=True)
    filename = f"oss-cad-suite-{host}-{DATE}.tgz"
    url = f"https://github.com/YosysHQ/oss-cad-suite-build/releases/download/{RELEASE}/{filename}"
    expected = {"release": RELEASE, "platform": host, "archive": filename, "sha256": HASHES[host], "source_url": url}
    install = tools / "oss-cad-suite"
    receipt = install / "installation.json"
    if install.exists():
        if not receipt.exists() or json.loads(receipt.read_text()).get("archive") != expected:
            parser.error(f"{install} already exists without the expected receipt; move it aside before installing")
    else:
        downloads = tools / "downloads"
        downloads.mkdir(exist_ok=True)
        archive = downloads / filename
        if not archive.exists():
            partial = archive.with_suffix(".partial")
            print(f"Downloading {url}", flush=True)
            with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            partial.replace(archive)
        actual = digest(archive)
        if actual != HASHES[host]:
            raise RuntimeError(f"archive checksum mismatch: expected {HASHES[host]}, got {actual}; remove {archive} and retry")
        print(f"Verified SHA256 {actual}; extracting", flush=True)
        # The hash above authenticates the exact pinned official archive. Reject
        # absolute and parent-traversing members as an additional path safeguard.
        with tempfile.TemporaryDirectory(prefix="cad-install-", dir=tools) as temporary:
            staging = Path(temporary)
            with tarfile.open(archive) as package:
                for member in package.getmembers():
                    name = Path(member.name)
                    if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "oss-cad-suite":
                        raise RuntimeError(f"unexpected archive path: {member.name}")
                package.extractall(staging)
            (staging / "oss-cad-suite").rename(install)
        receipt.write_text(json.dumps({"archive": expected}, indent=2) + "\n")
    env = os.environ.copy()
    env["PATH"] = str(install / "bin") + os.pathsep + env.get("PATH", "")
    versions = {}
    for command in ("iverilog", "vvp", "yosys"):
        completed = subprocess.run([str(install / "bin" / command), "-V"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=True)
        versions[command] = completed.stdout.strip().splitlines()[0]
        print(versions[command])
    receipt.write_text(json.dumps({"archive": expected, "versions": versions}, indent=2) + "\n")
    print(f"Installed locally at {install}")
    print(f'export PATH="{install / "bin"}:$PATH"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

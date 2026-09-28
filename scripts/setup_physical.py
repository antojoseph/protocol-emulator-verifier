#!/usr/bin/env python3
"""Install immutable official CMOS5L dependencies (host Python 3.9+ only)."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "verifier/physical_support/flow-lock.json").read_text())
WHEELS = json.loads((ROOT / "verifier/physical_support/python-wheels.json").read_text())


def run(*args, **kwargs):
    print("+", " ".join(map(str,args)), flush=True)
    subprocess.run(list(map(str,args)), check=True, **kwargs)


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def checkout(spec, path):
    if path.exists():
        revision = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], text=True, capture_output=True)
        if revision.returncode == 0:
            if revision.stdout.strip() != spec["commit"]:
                raise SystemExit(f"Refusing to change existing dependency with unexpected revision: {path}")
            run("git", "-C", path, "diff", "HEAD", "--exit-code")
            return
        if any(p.name != '.git' for p in path.iterdir()):
            raise SystemExit(f"Refusing to overwrite existing non-checkout directory: {path}")
    else:
        path.mkdir(parents=True)
    run("git", "-C", path, "init", "-q")
    run("git", "-C", path, "fetch", "--depth=1", spec["repository"], spec["commit"])
    run("git", "-C", path, "checkout", "--detach", "FETCH_HEAD")


def install_wheels(root):
    wheels = root / 'wheels'
    packages = root / 'precheck-python'
    wheels.mkdir(exist_ok=True)
    packages.mkdir(exist_ok=True)
    for spec in WHEELS['packages']:
        target = wheels / spec['filename']
        if not target.is_file():
            temporary = target.with_suffix('.download')
            print(f"Downloading pinned {spec['name']} {spec['version']} Linux wheel", flush=True)
            with urllib.request.urlopen(spec['url'], timeout=60) as source, temporary.open('wb') as output:
                while chunk := source.read(1024*1024):
                    output.write(chunk)
            if sha256(temporary) != spec['sha256']:
                temporary.unlink()
                raise SystemExit(f"Wheel checksum mismatch: {spec['filename']}")
            temporary.replace(target)
        if sha256(target) != spec['sha256']:
            raise SystemExit(f"Cached wheel checksum mismatch: {target}")
        with zipfile.ZipFile(target) as archive:
            for item in archive.infolist():
                relative = Path(item.filename)
                if relative.is_absolute() or '..' in relative.parts or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                    raise SystemExit(f"Unsafe wheel entry: {item.filename}")
                if item.is_dir():
                    continue
                destination = packages / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(item)
                if destination.exists() and destination.read_bytes() != data:
                    raise SystemExit(f"Refusing changed cached wheel content: {destination}")
                if not destination.exists():
                    destination.write_bytes(data)
    return packages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-root", type=Path, default=ROOT / ".tools/physical")
    parser.add_argument("--skip-image", action="store_true", help="Skip image pull; pinned image must already exist for the dependency smoke test")
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        raise SystemExit("Use host Python 3.9 or later")
    root = args.tools_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    checkout(LOCK["support_tools"], root / "tt")
    checkout(LOCK["pdk"], root / "pdk")
    (root / "pdk" / LOCK["pdk"]["name"] / "SOURCES").write_text(f"IHP-Open-PDK {LOCK['pdk']['commit']}\n")
    packages = install_wheels(root)
    run("docker", "info", "--format", "{{.ServerVersion}}", timeout=20)
    if not args.skip_image:
        run("docker", "pull", "--platform=linux/amd64", LOCK["container"])
    expected = {spec['name']: spec['version'] for spec in WHEELS['packages']}
    code = ("import sys,json,importlib.metadata,gdstk,klayout.db,klayout.rdb,yaml; "
            "assert sys.version_info[:2]==(3,13); "
            "versions={name:importlib.metadata.version(name) for name in " + repr(list(expected)) + "}; "
            "assert versions==" + repr(expected) + "; print(json.dumps(versions))")
    run("docker", "run", "--rm", "--platform=linux/amd64", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=16g", "--cpus=4", "--pids-limit=2048",
        "--tmpfs", "/tmp:rw,size=1g", "--user", f"{os.getuid()}:{os.getgid()}",
        "--volume", f"{packages}:{packages}:ro", "--env", f"PYTHONPATH={packages}",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", f"LD_LIBRARY_PATH={WHEELS['container_library_path']}", LOCK["container"], "python3", "-c", code, timeout=60)
    print(f"Physical dependencies prepared in {root}; no host EDA/Python packages installed")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check that an accepted candidate's exact source is anonymously public on GitHub.

The result file and its adjacent candidate snapshot must come from the trusted
organizer runner. This checks source availability; it does not submit anything,
authenticate a participant-supplied result, or establish official acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verifier.candidate import validate
from verifier.cli import harness_hashes
from verifier.common import VerificationError, digest, finite_number, read_json, write_json

MAX_COMPRESSED = 64 * 1024 * 1024
MAX_DECOMPRESSED = 256 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_DOWNLOAD_SECONDS = 120
DEADLINE = date(2027, 1, 18)


class PublicationError(VerificationError):
    pass


def repository_coordinates(repository, revision, subdirectory):
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})/?", repository)
    if not match or match.group(2) in (".", ".."):
        raise PublicationError("repository must be an HTTPS GitHub owner/repository URL without credentials, query or fragment")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise PublicationError("revision must be an exact 40-character Git commit SHA")
    if not isinstance(subdirectory, str) or not subdirectory:
        raise PublicationError("subdirectory is required; use . for repository root")
    path = PurePosixPath(subdirectory)
    if path.is_absolute() or ".." in path.parts or "\\" in subdirectory or any(ord(c) < 32 for c in subdirectory):
        raise PublicationError("unsafe repository subdirectory")
    canonical = path.as_posix()
    if subdirectory.rstrip("/") != canonical:
        raise PublicationError("subdirectory must use a canonical relative POSIX path")
    return match.group(1), match.group(2), revision.lower(), "" if canonical == "." else canonical + "/"


def accepted_snapshot(result_path):
    path = Path(result_path).resolve()
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise PublicationError("missing or oversized trusted result file")
    result = read_json(path)
    if (result.get("schema_version") != 1 or result.get("contract") != "js-protocol-emulator-v1"
            or result.get("mode") != "full" or result.get("status") != "pass"
            or result.get("accepted") is not True):
        raise PublicationError("publication requires a completed, accepted full verifier result")
    finite_number(result.get("score"), "full score", positive=True)
    checks = {check.get("name"): check.get("status") for check in result.get("checks", [])}
    for name in ("source_license_manifest", "source_and_harness_unchanged"):
        if checks.get(name) != "pass":
            raise PublicationError("trusted result lacks required acceptance check: " + name)
    for name in ("synthesis", "functional", "physical", "gates"):
        if result.get("stages", {}).get(name, {}).get("status") != "pass":
            raise PublicationError("trusted full result lacks passing stage: " + name)
    if result.get("harness_sha256") != harness_hashes():
        raise PublicationError("result was not produced by the exact current trusted verifier revision")
    expected = result.get("candidate", {}).get("source_sha256")
    if not isinstance(expected, dict) or not expected or len(expected) > 256:
        raise PublicationError("missing or invalid accepted source hash manifest")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in expected.values()):
        raise PublicationError("invalid accepted SHA256 value")
    snapshot = path.parent / "candidate"
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise PublicationError("trusted result must have its adjacent candidate snapshot")
    manifest, actual = validate(snapshot)
    if actual != expected:
        raise PublicationError("accepted candidate snapshot differs from recorded source hashes")
    return result, manifest, expected


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicationError("anonymous archive request unexpectedly redirected")


@contextmanager
def download_archive(url):
    # A fresh opener uses neither ambient auth/cookies nor environment proxies.
    # GitHub codeload serves public archives without a credential exchange.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "protocol-emulator-publication-check/1", "Accept": "application/gzip"})
    with opener.open(request, timeout=30) as response, tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as archive:
        if response.status != 200 or response.geturl() != url:
            raise PublicationError("exact public archive request did not return HTTP 200")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_COMPRESSED):
            raise PublicationError("archive exceeds compressed download budget")
        total = 0
        started = time.monotonic()
        h = hashlib.sha256()
        while True:
            if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                raise PublicationError("archive download exceeded wall-clock budget")
            block = response.read1(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_COMPRESSED:
                raise PublicationError("archive exceeds compressed download budget")
            h.update(block)
            archive.write(block)
        if not total:
            raise PublicationError("public archive was empty")
        archive.seek(0)
        yield archive, {"archive_sha256": h.hexdigest(), "compressed_bytes": total}


class BoundedReader:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size=-1):
        remaining = self.limit - self.count
        wanted = remaining + 1 if size < 0 else min(size, remaining + 1)
        result = self.stream.read(wanted)
        self.count += len(result)
        if self.count > self.limit:
            raise PublicationError("archive exceeds decompressed byte budget")
        return result


def check_archive(archive, repository_name, revision, prefix, expected):
    """Stream a bounded tar.gz without extraction, imports, or candidate execution."""
    seen, found, roots = set(), {}, set()
    members = total_file_bytes = 0
    with gzip.GzipFile(fileobj=archive, mode="rb") as gzip_stream:
        decoded = BoundedReader(gzip_stream, MAX_DECOMPRESSED)
        with tarfile.open(fileobj=decoded, mode="r|") as package:
            for member in package:
                members += 1
                if members > MAX_MEMBERS:
                    raise PublicationError("archive exceeds member-count budget")
                name = member.name.rstrip("/")
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                        or any(ord(c) < 32 for c in name) or path.as_posix() != name):
                    raise PublicationError("unsafe archive member path")
                if name in seen:
                    raise PublicationError("duplicate archive member path")
                seen.add(name)
                roots.add(path.parts[0])
                if len(roots) != 1 or path.parts[0].casefold() != (repository_name + "-" + revision).casefold():
                    raise PublicationError("archive root does not match exact repository revision")
                if member.issym() or member.islnk() or member.issparse() or not (member.isdir() or member.isfile()):
                    raise PublicationError("archive links, sparse files and special files are forbidden")
                total_file_bytes += member.size
                if member.size < 0 or total_file_bytes > MAX_DECOMPRESSED:
                    raise PublicationError("archive file sizes exceed budget")
                relative = "/".join(path.parts[1:])
                if member.isdir() or not relative.startswith(prefix):
                    continue
                candidate_name = relative[len(prefix):]
                if candidate_name not in expected:
                    continue
                stream = package.extractfile(member)
                if stream is None:
                    raise PublicationError("expected public source was not a regular file")
                h = hashlib.sha256()
                with stream:
                    for block in iter(lambda: stream.read(64 * 1024), b""):
                        h.update(block)
                actual = h.hexdigest()
                if actual != expected[candidate_name]:
                    raise PublicationError("public source differs from accepted candidate: " + candidate_name)
                found[candidate_name] = actual
        # Consume the gzip trailer too, checking truncation/CRC and the complete
        # decompression bound even when tar's end marker occurs earlier.
        while decoded.read(64 * 1024):
            pass
    missing = sorted(set(expected) - set(found))
    if not expected or missing:
        raise PublicationError("accepted files missing from public archive: " + ", ".join(missing[:10]))
    return {"matched_files": len(found), "source_sha256": found, "archive_members": members,
            "decompressed_bytes": decoded.count}


def deadline_calendar(now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone.utc).date()
    state = "before_deadline_date" if day < DEADLINE else "on_deadline_date" if day == DEADLINE else "after_deadline_date"
    return {"published_deadline_date": DEADLINE.isoformat(), "checked_utc_date": day.isoformat(),
            "calendar_status": state, "organizer_timezone": "unspecified in blog",
            "exact_cutoff_verified": False, "submission_by_deadline_verified": False}


def check_publication(result_path, repository, revision, subdirectory):
    owner, repo, revision, prefix = repository_coordinates(repository, revision, subdirectory)
    result, manifest, expected = accepted_snapshot(result_path)
    result_digest = digest(result_path)
    url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{revision}"
    with download_archive(url) as (archive, download):
        matching = check_archive(archive, repo, revision, prefix, expected)
    if digest(result_path) != result_digest or accepted_snapshot(result_path)[2] != expected:
        raise PublicationError("trusted result or candidate snapshot changed during publication checking")
    return {"schema_version": 1, "status": "pass", "technical_acceptance": True,
            "public_source_matches_accepted_snapshot": True, "license": manifest["license"],
            "repository": f"https://github.com/{owner}/{repo}", "revision": revision,
            "subdirectory": prefix.rstrip("/") or ".", "anonymous_archive_url": url,
            "result_sha256": result_digest, "checker_sha256": digest(__file__),
            "accepted_score": result["score"], **download, **matching,
            "checked_utc": datetime.now(timezone.utc).isoformat(), "deadline": deadline_calendar(),
            "official_submission_verified": False, "organizer_acceptance_or_prize_verified": False,
            "scope": "Exact accepted source is publicly downloadable without credentials. Result authenticity relies on organizer-controlled evidence; official submission is an external event."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--subdirectory", required=True)
    parser.add_argument("--out", type=Path, help="Default: publication-check.json beside the full result")
    args = parser.parse_args()
    output = (args.out or args.result.resolve().parent / "publication-check.json").resolve()
    snapshot = args.result.resolve().parent / "candidate"
    if output == args.result.resolve() or output == snapshot or output.is_relative_to(snapshot):
        parser.error("publication output must not overwrite the full result or its candidate snapshot")
    report = {"schema_version": 1, "status": "running", "technical_acceptance": False,
              "public_source_matches_accepted_snapshot": False, "official_submission_verified": False}
    write_json(output, report)
    try:
        report = check_publication(args.result, args.repository, args.revision, args.subdirectory)
    except Exception as error:
        report.update(status="fail", error=str(error), deadline=deadline_calendar())
    write_json(output, report)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Publication tests use fabricated trusted evidence and in-memory archives only."""
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_publication as pub
from verifier.candidate import validate
from verifier.common import ROOT, VerificationError

REVISION = "a" * 40
REPOSITORY = "https://github.com/example/challenge"
ARCHIVE_ROOT = "challenge-" + REVISION
HARNESS = {"verifier/test-fixture.py": "b" * 64}


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, contents, kind in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = contents.decode()
            elif kind == tarfile.REGTYPE:
                entry.size = len(contents)
            archive.addfile(entry, io.BytesIO(contents) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate"
        shutil.copytree(ROOT / "candidates/tempo", self.candidate)
        manifest, self.hashes = validate(self.candidate)
        self.result = {"schema_version": 1, "contract": "js-protocol-emulator-v1", "status": "pass",
                       "mode": "full", "accepted": True, "score": 2.0,
                       "checks": [{"name": name, "status": "pass"} for name in ("source_license_manifest", "source_and_harness_unchanged")],
                       "stages": {name: {"status": "pass"} for name in ("synthesis", "functional", "physical", "gates")},
                       "candidate": {"name": manifest["name"], "source_sha256": self.hashes},
                       "harness_sha256": HARNESS}
        self.result_path = self.root / "result.json"
        self.write_result()
        self.patcher = patch.object(pub, "harness_hashes", return_value=HARNESS)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_result(self):
        self.result_path.write_text(json.dumps(self.result))

    def entries(self):
        return [(ARCHIVE_ROOT + "/", b"", tarfile.DIRTYPE)] + [
            (ARCHIVE_ROOT + "/candidates/tempo/" + name, (self.candidate / name).read_bytes(), tarfile.REGTYPE)
            for name in self.hashes]

    def check_archive(self, entries):
        return pub.check_archive(io.BytesIO(archive_bytes(entries)), "challenge", REVISION, "candidates/tempo/", self.hashes)

    def test_matching_public_archive_and_full_result_pass_without_submission_claim(self):
        data = archive_bytes(self.entries())
        @contextmanager
        def fake_download(url):
            self.assertEqual(url, f"https://codeload.github.com/example/challenge/tar.gz/{REVISION}")
            yield io.BytesIO(data), {"archive_sha256": hashlib.sha256(data).hexdigest(), "compressed_bytes": len(data)}
        with patch.object(pub, "download_archive", side_effect=fake_download), patch.object(tarfile.TarFile, "extractall") as extraction:
            report = pub.check_publication(self.result_path, REPOSITORY, REVISION, "candidates/tempo")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["matched_files"], len(self.hashes))
        self.assertTrue(report["public_source_matches_accepted_snapshot"])
        self.assertFalse(report["official_submission_verified"])
        self.assertFalse(report["organizer_acceptance_or_prize_verified"])
        extraction.assert_not_called()

    def test_fast_failed_incomplete_or_forged_score_is_rejected_before_network(self):
        original = json.loads(json.dumps(self.result))
        for updates in ({"mode": "fast"}, {"accepted": False}, {"accepted": 1}, {"status": "running"},
                        {"score": None}, {"score": float("nan")}, {"score": True}, {"stages": {}},
                        {"checks": []}, {"harness_sha256": {}}, {"candidate": {"source_sha256": {}}}):
            self.result = {**original, **updates}
            self.write_result()
            with self.subTest(updates=updates), patch.object(pub, "download_archive") as download:
                with self.assertRaises((VerificationError, ValueError)):
                    pub.check_publication(self.result_path, REPOSITORY, REVISION, "candidates/tempo")
                download.assert_not_called()

    def test_accepted_snapshot_source_and_license_must_still_match(self):
        for name in ("adapter.py", "LICENSE"):
            path = self.candidate / name
            original = path.read_bytes()
            path.write_bytes(b"changed")
            try:
                with self.subTest(name=name), self.assertRaises(VerificationError):
                    pub.accepted_snapshot(self.result_path)
            finally:
                path.write_bytes(original)

    def test_snapshot_change_during_download_is_rejected(self):
        data = archive_bytes(self.entries())
        @contextmanager
        def changing_download(url):
            yield io.BytesIO(data), {"archive_sha256": hashlib.sha256(data).hexdigest(), "compressed_bytes": len(data)}
            with (self.candidate / "adapter.py").open("ab") as source:
                source.write(b"\n# changed during download\n")
        with patch.object(pub, "download_archive", side_effect=changing_download):
            with self.assertRaisesRegex(VerificationError, "differs|changed"):
                pub.check_publication(self.result_path, REPOSITORY, REVISION, "candidates/tempo")

    def test_public_missing_or_modified_files_fail_closed(self):
        entries = self.entries()
        with self.assertRaisesRegex(pub.PublicationError, "missing"):
            self.check_archive(entries[:-1])
        name, data, kind = entries[-1]
        entries[-1] = (name, data + b"changed", kind)
        with self.assertRaisesRegex(pub.PublicationError, "differs"):
            self.check_archive(entries)
        with self.assertRaises(pub.PublicationError):
            self.check_archive([])

    def test_archive_traversal_links_duplicates_and_special_files_rejected(self):
        bad_entries = [
            ("/absolute", b"x", tarfile.REGTYPE),
            (ARCHIVE_ROOT + "/../escape", b"x", tarfile.REGTYPE),
            (ARCHIVE_ROOT + "/a\\escape", b"x", tarfile.REGTYPE),
            (ARCHIVE_ROOT + "/link", b"/etc/passwd", tarfile.SYMTYPE),
            (ARCHIVE_ROOT + "/hardlink", b"/etc/passwd", tarfile.LNKTYPE),
            (ARCHIVE_ROOT + "/fifo", b"", tarfile.FIFOTYPE),
            ("another-root/file", b"x", tarfile.REGTYPE),
            self.entries()[-1],
        ]
        for bad in bad_entries:
            with self.subTest(entry=bad[0]), self.assertRaises(pub.PublicationError):
                self.check_archive(self.entries() + [bad])

    def test_archive_budgets_and_truncated_gzip_fail(self):
        data = archive_bytes(self.entries())
        with patch.object(pub, "MAX_MEMBERS", 1), self.assertRaises(pub.PublicationError):
            self.check_archive(self.entries())
        with patch.object(pub, "MAX_DECOMPRESSED", 100), self.assertRaises(pub.PublicationError):
            self.check_archive(self.entries())
        with self.assertRaises((EOFError, OSError, tarfile.TarError)):
            pub.check_archive(io.BytesIO(data[:-16]), "challenge", REVISION, "candidates/tempo/", self.hashes)

    def test_repository_requires_credential_free_url_and_immutable_revision(self):
        bad = [("https://user:secret@github.com/example/challenge", REVISION, "."),
               (REPOSITORY + "?token=secret", REVISION, "."),
               ("https://github.com.evil.test/example/challenge", REVISION, "."),
               (REPOSITORY, "main", "."), (REPOSITORY, REVISION, "../escape"),
               (REPOSITORY, REVISION, "/absolute"), (REPOSITORY, REVISION, "a/./b")]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(pub.PublicationError):
                pub.repository_coordinates(*values)
        self.assertEqual(pub.repository_coordinates(REPOSITORY, REVISION, ".")[-1], "")

    def test_download_has_no_ambient_auth_and_enforces_size_budget(self):
        data = archive_bytes(self.entries())
        url = f"https://codeload.github.com/example/challenge/tar.gz/{REVISION}"
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": str(len(data))}
            def geturl(self): return url
        class Opener:
            def open(inner, request, timeout):
                self.assertFalse(any(name.lower() in ("authorization", "cookie", "proxy-authorization") for name in request.headers))
                return Response(data)
        with patch.object(pub.urllib.request, "build_opener", return_value=Opener()) as build:
            with pub.download_archive(url) as (archive, metadata):
                self.assertEqual(archive.read(), data)
                self.assertEqual(metadata["compressed_bytes"], len(data))
            self.assertEqual(build.call_args.args[0].proxies, {})
        with patch.object(pub.urllib.request, "build_opener", return_value=Opener()), patch.object(pub, "MAX_COMPRESSED", 1):
            with self.assertRaises(pub.PublicationError):
                with pub.download_archive(url): pass

    def test_deadline_is_calendar_only_without_invented_timezone_or_cutoff(self):
        for day, expected in ((17, "before_deadline_date"), (18, "on_deadline_date"), (19, "after_deadline_date")):
            report = pub.deadline_calendar(datetime(2027, 1, day, 12, tzinfo=timezone.utc))
            self.assertEqual(report["calendar_status"], expected)
            self.assertFalse(report["exact_cutoff_verified"])
            self.assertFalse(report["submission_by_deadline_verified"])


if __name__ == "__main__":
    unittest.main()

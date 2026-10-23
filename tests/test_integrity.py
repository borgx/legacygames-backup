"""Offline integrity and backward-compatibility tests; no real HTTP calls."""
import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("legacygames_backup", ROOT / "legacygames_backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)

DATA = b"The installer bytes for this offline test.\x00\xff"
SHA = hashlib.sha256(DATA).hexdigest()
MD5 = hashlib.md5(DATA).hexdigest()
ETAG = '"' + MD5 + '"'
URL = "https://cdn.legacygames.com/test.exe"


def row_for(data=DATA):
    result = {name: "" for name in backup.COLUMNS}
    result.update(title="Test", source="purchased", game_id="g", product_id="p",
                  installer_uuid="u", filename="test.exe", download_url=URL)
    return result


class FakeResponse(io.BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "LegacyGamesBackup"
        self.installers = self.root / "installers"
        self.installers.mkdir(parents=True)

    def existing(self, data=DATA):
        file = self.installers / "test.exe"
        file.write_bytes(data)
        return file

    def test_etag_formats(self):
        self.assertEqual(backup.md5_from_etag(ETAG), MD5)
        self.assertEqual(backup.md5_from_etag(MD5.upper()), MD5)
        for bad in ('W/' + ETAG, '"' + MD5 + '-2"', '"' + 'a' * 64 + '"', ""):
            self.assertEqual(backup.md5_from_etag(bad), "")

    def test_existing_remote_md5_sha_size_verified(self):
        self.existing()
        row = row_for()
        row["sha256"] = SHA
        row["bytes"] = str(len(DATA))
        with patch.object(backup, "get_cdn_head", return_value=(ETAG, len(DATA), "")):
            status = backup.verify_existing(row, self.installers)
        self.assertEqual(status, "VERIFIED_MD5_ETAG")
        self.assertEqual(row["md5"], MD5)
        self.assertEqual(row["sha256"], SHA)
        self.assertEqual(row["cdn_etag"], ETAG)

    def test_saved_sha_mismatch_never_overwritten(self):
        self.existing(b"a locally modified installer")
        row = row_for()
        row["sha256"] = SHA
        with patch.object(backup, "get_cdn_head", return_value=("", None, "CDN HEAD unavailable")):
            status = backup.verify_existing(row, self.installers)
        self.assertEqual(status, "MISMATCH")
        self.assertEqual(row["sha256"], SHA)
        self.assertEqual(row["md5"], "")
        self.assertIn("saved SHA-256", row["error"])

    def test_remote_update_mismatch_but_local_hash_unchanged(self):
        self.existing()
        row = row_for()
        row["sha256"] = SHA
        changed_etag = '"' + ('0' * 32) + '"'
        with patch.object(backup, "get_cdn_head", return_value=(changed_etag, len(DATA), "")):
            status = backup.verify_existing(row, self.installers)
        self.assertEqual(status, "MISMATCH")
        self.assertEqual(row["sha256"], SHA)
        self.assertEqual(row["cdn_etag"], "")

    def test_local_only_verifies_saved_sha(self):
        self.existing()
        row = row_for()
        row["sha256"] = SHA
        with patch.object(backup, "get_cdn_head", side_effect=AssertionError("no HEAD allowed")):
            self.assertEqual(backup.verify_existing(row, self.installers, local_only=True), "LOCAL_SHA256_ONLY")

    def test_no_remote_and_no_historical_hash_remains_unverified(self):
        self.existing()
        row = row_for()
        with patch.object(backup, "get_cdn_head", return_value=("", None, "CDN HEAD unavailable")):
            self.assertEqual(backup.verify_existing(row, self.installers), "UNVERIFIED")
        self.assertEqual(row["sha256"], SHA)
        self.assertEqual(row["error"], "CDN HEAD unavailable")

    def test_old_csv_migration_preserves_original_order(self):
        legacy_row = {k: "" for k in backup.OLD_COLUMNS}
        legacy_row.update(title="Test", filename="test.exe", download_url=URL, sha256=SHA,
                          bytes=str(len(DATA)))
        self.existing()
        self.root.mkdir(exist_ok=True)
        path = self.root / "legacygames_links.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as output:
            w = csv.DictWriter(output, fieldnames=backup.OLD_COLUMNS)
            w.writeheader()
            w.writerow(legacy_row)
        rows = backup.read_saved_links(self.root)
        self.assertEqual(rows[0]["sha256"], SHA)
        with patch.object(backup, "get_cdn_head", return_value=(ETAG, len(DATA), "")):
            backup.run_verification(rows, self.installers, output=self.root)
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, backup.COLUMNS)
            self.assertEqual(next(reader)["verification_status"], "VERIFIED_MD5_ETAG")
        self.assertTrue((self.root / "legacygames_links.csv.bak").exists())
        self.assertEqual(backup.read_saved_links(self.root)[0]["sha256"], SHA)

    def test_refresh_keeps_old_hashes(self):
        old = row_for()
        old.update(sha256=SHA, md5=MD5, cdn_etag=ETAG, cdn_bytes=str(len(DATA)))
        new = row_for()
        new["download_url"] = URL + "?token=new"
        backup.merge_prior_verification([new], [old])
        self.assertEqual(new["sha256"], SHA)
        self.assertEqual(new["md5"], MD5)
        self.assertEqual(new["download_url"], URL + "?token=new")
        new2 = row_for()
        new2["filename"] = "new-version.exe"
        backup.merge_prior_verification([new2], [old])
        self.assertEqual(new2["sha256"], "")

    def test_download_verified_before_promoting_partial(self):
        row = row_for()
        headers = {"Content-Length": str(len(DATA)), "ETag": ETAG, "Content-Type": "application/x-msdownload"}
        with patch.object(backup, "urlopen", return_value=FakeResponse(DATA, headers=headers)):
            backup.download_one(row, self.installers)
        self.assertEqual(row["verification_status"], "VERIFIED_MD5_ETAG")
        self.assertEqual(row["sha256"], SHA)
        self.assertEqual((self.installers / "test.exe").read_bytes(), DATA)
        self.assertFalse((self.installers / "test.exe.part").exists())

    def test_download_mismatch_keeps_partial(self):
        row = row_for()
        headers = {"Content-Length": str(len(DATA)), "ETag": '"' + ('0' * 32) + '"'}
        with patch.object(backup, "urlopen", return_value=FakeResponse(DATA, headers=headers)):
            with self.assertRaisesRegex(RuntimeError, "failed integrity validation"):
                backup.download_one(row, self.installers)
        self.assertEqual(row["verification_status"], "MISMATCH")
        self.assertFalse((self.installers / "test.exe").exists())
        self.assertEqual((self.installers / "test.exe.part").read_bytes(), DATA)

    def test_resume_checks_full_payload(self):
        row = row_for()
        half = len(DATA) // 2
        (self.installers / "test.exe.part").write_bytes(DATA[:half])
        (self.installers / "test.exe.part.json").write_text(json.dumps({
            "url_identity": URL, "etag": ETAG, "total_bytes": len(DATA),
            "last_modified": None,
        }))
        rest = DATA[half:]
        headers = {"Content-Length": str(len(rest)), "Content-Range":
                   f"bytes {half}-{len(DATA)-1}/{len(DATA)}", "ETag": ETAG}
        with patch.object(backup, "urlopen", return_value=FakeResponse(rest, status=206, headers=headers)) as send:
            backup.download_one(row, self.installers)
        self.assertEqual(row["verification_status"], "VERIFIED_MD5_ETAG")
        self.assertEqual(row["status"], "downloaded (resumed)")
        self.assertEqual((self.installers / "test.exe").read_bytes(), DATA)
        req = send.call_args.args[0]
        self.assertEqual(req.headers.get("Range"), f"bytes={half}-")

    def test_mismatch_does_not_overwrite_existing_installer(self):
        self.existing()
        row = row_for()
        row["sha256"] = "0" * 64
        with patch.object(backup, "get_cdn_head", return_value=(ETAG, len(DATA), "")):
            with patch.object(backup, "urlopen", side_effect=AssertionError("no download")):
                backup.download_one(row, self.installers)
        self.assertEqual((self.installers / "test.exe").read_bytes(), DATA)
        self.assertEqual(row["status"], "integrity mismatch")
        self.assertEqual(row["sha256"], "0" * 64)

    def test_invalid_filename_never_reads_outside_installers(self):
        row = row_for()
        row["filename"] = "../secret.exe"
        self.assertEqual(backup.verify_existing(row, self.installers), "INVALID_FILENAME")
        with self.assertRaises(RuntimeError):
            backup.download_one(row, self.installers)

    def test_head_uses_only_head_method(self):
        with patch.object(backup.HEAD_OPENER, "open", return_value=FakeResponse(
            b"", headers={"ETag": ETAG, "Content-Length": str(len(DATA))})) as request:
            etag, size, error = backup.get_cdn_head(URL)
        self.assertEqual((etag, size, error), (ETAG, len(DATA), ""))
        self.assertEqual(request.call_args.args[0].get_method(), "HEAD")

    def test_non_md5_etag_is_not_trusted(self):
        self.existing()
        row = row_for()
        row["sha256"] = SHA
        with patch.object(backup, "get_cdn_head", return_value=('W/' + ETAG, len(DATA), "")):
            self.assertEqual(backup.verify_existing(row, self.installers), "LOCAL_SHA256_ONLY")

    def test_bad_cdn_size_is_a_mismatch(self):
        self.existing()
        row = row_for()
        row["sha256"] = SHA
        with patch.object(backup, "get_cdn_head", return_value=(ETAG, len(DATA) + 3, "")):
            self.assertEqual(backup.verify_existing(row, self.installers), "MISMATCH")
        self.assertEqual(row["sha256"], SHA)
        self.assertEqual(row["cdn_bytes"], "")

    def test_verify_mismatch_exit_code_and_saved_hash_preserved(self):
        self.existing(b"changed")
        row = row_for()
        row["sha256"] = SHA
        backup.write_exports([row], self.root)
        with patch.object(backup, "get_cdn_head", return_value=(ETAG, len(DATA), "")):
            code = backup.main(["--verify", "--output", str(self.root)])
        self.assertEqual(code, 2)
        saved = backup.read_saved_links(self.root)[0]
        self.assertEqual(saved["sha256"], SHA)
        self.assertEqual(saved["verification_status"], "MISMATCH")

    def test_main_verify_requires_no_authentication(self):
        self.existing()
        row = row_for()
        row["sha256"] = SHA
        backup.write_exports([row], self.root)
        with patch.object(backup, "get_cdn_head", return_value=(ETAG, len(DATA), "")):
            with patch("builtins.input", side_effect=AssertionError("no login")):
                rc = backup.main(["--verify", "--output", str(self.root)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

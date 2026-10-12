"""Local byte fixtures only; no network, real models or credentials."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cellivane_seed_assets as assets


class Response(BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = {} if headers is None else headers


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.body = b"tiny-local-gguf-byte-fixture"
        self.spec = {"repo": "Qwen/fixture-only", "revision": "a" * 40,
            "filename": "fixture.gguf", "size": len(self.body), "sha256": sha256(self.body).hexdigest()}

    def acquire(self, body, headers=None):
        return assets.acquire(self.spec, self.root, opener=lambda *_args, **_kwargs: Response(body, headers))

    def assert_preserved_failure(self, report):
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse((self.root / "models" / "fixture.gguf").exists())
        self.assertTrue(Path(report["partial_path"]).is_file())
        self.assertTrue(Path(report["receipt_path"]).is_file())

    def test_verified_bytes_are_published_without_execution(self):
        report = self.acquire(self.body)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(Path(report["path"]).read_bytes(), self.body)
        self.assertFalse(Path(report["partial_path"]).exists())
        self.assertFalse(report["model_executed"])

    def test_wrong_hash_never_publishes_and_preserves_partial(self):
        self.assert_preserved_failure(self.acquire(b"X" * len(self.body)))

    def test_truncated_body_never_publishes(self):
        self.assert_preserved_failure(self.acquire(self.body[:-1]))

    def test_oversized_body_is_bounded_before_publication(self):
        report = self.acquire(self.body + b"overflow")
        self.assert_preserved_failure(report)
        self.assertLessEqual(Path(report["partial_path"]).stat().st_size, self.spec["size"])

    def test_wrong_content_length_never_publishes(self):
        self.assert_preserved_failure(self.acquire(self.body, {"Content-Length": str(len(self.body) + 1)}))

    def test_existing_mismatch_is_not_overwritten(self):
        target = self.root / "models" / "fixture.gguf"
        target.parent.mkdir()
        target.write_bytes(b"preserve-existing")
        report = self.acquire(self.body)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(target.read_bytes(), b"preserve-existing")
        self.assertFalse(Path(report["partial_path"]).exists())

    def test_existing_match_is_reverified_without_network(self):
        target = self.root / "models" / "fixture.gguf"
        target.parent.mkdir()
        target.write_bytes(self.body)
        def forbidden(*_args, **_kwargs):
            raise AssertionError("existing match must not contact network")
        report = assets.acquire(self.spec, self.root, opener=forbidden)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["action"], "verified-existing")


if __name__ == "__main__":
    unittest.main()

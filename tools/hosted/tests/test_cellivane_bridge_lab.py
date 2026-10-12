"""Cache corruption negatives use tiny synthetic archives, never live assets."""
from hashlib import sha256
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

LAB = Path(__file__).resolve().parents[1] / "cellivane_bridge_lab.py"
spec = importlib.util.spec_from_file_location("cellivane_cache_test_lab", LAB)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


@unittest.skipUnless(lab.os.name == "nt", "launcher is Windows-only")
class CacheIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "downloads" / ("ollama-v" + lab.OLLAMA_VERSION + "-windows-amd64.zip")
        self.archive.parent.mkdir()
        with zipfile.ZipFile(self.archive, "w") as package:
            package.writestr("ollama.exe", b"synthetic executable")
            package.writestr("lib/ollama/ggml-cpu-test.dll", b"synthetic dependency")
        for patcher in (patch.object(lab, "OLLAMA_ZIP_BYTES", self.archive.stat().st_size),
                        patch.object(lab, "OLLAMA_ZIP_SHA", sha256(self.archive.read_bytes()).hexdigest()),
                        patch.object(lab, "pinned_copy", side_effect=lambda src, target, expected: target)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine_dir = self.root / "engines" / ("ollama-" + lab.OLLAMA_VERSION)

    def test_every_member_is_checked_on_reuse(self):
        self.assertEqual(lab.prepare(self.root, self.root)["ollama_all_extracted_members_verified"], 2)
        self.assertEqual(lab.prepare(self.root, self.root)["ollama_all_extracted_members_verified"], 2)
        dll = self.engine_dir / "lib" / "ollama" / "ggml-cpu-test.dll"
        dll.write_bytes(b"x" * dll.stat().st_size)
        with self.assertRaisesRegex(ValueError, "member-hash-mismatch"):
            lab.prepare(self.root, self.root)

    def test_unexpected_loadable_file_is_rejected(self):
        lab.prepare(self.root, self.root)
        (self.engine_dir / "extra.dll").write_bytes(b"fixture")
        with self.assertRaisesRegex(ValueError, "extra-or-missing"):
            lab.prepare(self.root, self.root)

    def test_missing_member_is_rejected(self):
        lab.prepare(self.root, self.root)
        (self.engine_dir / "lib" / "ollama" / "ggml-cpu-test.dll").unlink()
        with self.assertRaisesRegex(ValueError, "member-missing"):
            lab.prepare(self.root, self.root)

    def test_incomplete_extraction_is_not_reused(self):
        self.engine_dir.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "incomplete-extraction"):
            lab.prepare(self.root, self.root)

    def test_mutated_archive_is_rejected_before_extract(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"x")
        with self.assertRaisesRegex(ValueError, "archive-digest"):
            lab.prepare(self.root, self.root)

    def test_parent_traversal_in_archive_is_rejected(self):
        with zipfile.ZipFile(self.archive, "w") as package:
            package.writestr("../escaped.dll", b"fixture")
        with patch.object(lab, "OLLAMA_ZIP_BYTES", self.archive.stat().st_size), patch.object(lab, "OLLAMA_ZIP_SHA", sha256(self.archive.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(ValueError, "unsafe-archive"):
                lab.prepare(self.root, self.root)
        self.assertFalse((self.root / "engines" / "escaped.dll").exists())


if __name__ == "__main__":
    unittest.main()

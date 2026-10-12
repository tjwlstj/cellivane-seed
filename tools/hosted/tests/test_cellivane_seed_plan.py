"""Planning CLI fixtures use tiny synthetic files and never run a backend."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/hosted"))
import cellivane_seed_plan as cli


class PlanningCommandTests(unittest.TestCase):
    def invoke(self, folder, **options):
        target = Path(folder) / "synthetic.gguf"
        target.write_bytes(b"fixture-model-not-a-real-gguf")
        values = {"backend": "llama.cpp", "engine-identity": "a" * 64,
            "target-file": str(target), "ram-available-bytes": "1024", "ram-headroom-bytes": "64",
            "host-overhead-bytes": "32", "context-workspace-bytes": "32", **options}
        argv = [item for key, value in values.items() for item in ("--" + key, str(value))]
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_declared_cpu_admission_never_dispatches_or_claims_os_enforcement(self):
        with tempfile.TemporaryDirectory() as folder:
            code, value = self.invoke(folder)
        self.assertEqual(code, 0)
        self.assertEqual(value["plan"]["outcome"], "ADMITTED")
        self.assertIs(value["backend_dispatched"], False)
        self.assertIs(value["plan"]["os_memory_limit_enforced"], False)

    def test_insufficient_ram_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            code, value = self.invoke(folder, **{"ram-available-bytes": "64"})
        self.assertEqual(code, 2)
        self.assertEqual(value["plan"]["reason"], "ram-budget-insufficient")

    def test_gpu_without_backend_estimate_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            code, value = self.invoke(folder, placement="hybrid")
        self.assertEqual(code, 2)
        self.assertEqual(value["plan"]["outcome"], "UNKNOWN")

    def test_ollama_ngram_request_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            code, value = self.invoke(folder, backend="ollama", mode="ngram")
        self.assertEqual(code, 2)
        self.assertEqual(value["plan"]["reason"], "capability-mode-unsupported")

    def test_draft_file_is_required_and_only_valid_for_draft_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            for options in ({"mode": "draft"}, {"draft-file": str(Path(folder) / "unused.gguf")}):
                code, value = self.invoke(folder, **options)
                self.assertEqual(code, 1)
                self.assertEqual(value["reason"], "draft-file-required-only-for-draft-mode")

    def test_synthetic_target_content_is_bound_in_plan_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            first_code, first = self.invoke(folder)
            second_code, second = self.invoke(folder, **{"context-tokens": "1024"})
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertNotEqual(first["identity"], second["identity"])


if __name__ == "__main__":
    unittest.main()

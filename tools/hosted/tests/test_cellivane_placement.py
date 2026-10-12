"""Pure placement fixtures; no subprocess, model, network or hardware access."""
from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "hosted/experimental"))
from cellivane_seed.placement import PlacementPlan, artifact_identity, build_plan, observe_placement


class PlacementPlanTests(unittest.TestCase):
    def test_three_profiles_have_fixed_bounded_parameters(self):
        for profile, gpu in (("cpu", 0), ("hybrid", 14), ("auto", -1)):
            with self.subTest(profile=profile):
                plan = build_plan(profile)
                self.assertEqual(plan.ollama_options(), {"num_ctx": 2048, "num_thread": 4,
                                                        "num_gpu": gpu, "temperature": 0})
                self.assertEqual((plan.backend, plan.slots), ("ollama", 1))
                self.assertEqual(plan.record()["scope"], "requested-placement-only")
                self.assertNotIn("gpu_offload_observed", plan.record())

    def test_hybrid_layer_bounds(self):
        for layers in (1, 128):
            self.assertEqual(build_plan("hybrid", layers).num_gpu, layers)
        for layers in (-1, 0, 129):
            with self.subTest(layers=layers), self.assertRaises(ValueError):
                build_plan("hybrid", layers)

    def test_invalid_profile_has_no_fallback(self):
        for profile in ("GPU", "cpu ", "cuda", "", None, True, 0, ["cpu"]):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                build_plan(profile)

    def test_layers_reject_bool_fraction_nonfinite_and_string_even_if_unused(self):
        for profile in ("cpu", "hybrid", "auto"):
            for layers in (True, False, 14.0, 1.5, float("nan"), float("inf"), "14", None):
                with self.subTest(profile=profile, layers=layers), self.assertRaises(ValueError):
                    build_plan(profile, layers)

    def test_direct_construction_cannot_override_fixed_contract(self):
        for fields in ({"backend": "llamafile"}, {"slots": True}, {"num_thread": 8},
                       {"num_ctx": 4096}, {"temperature": False}, {"schema_version": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                PlacementPlan("cpu", 0, **fields)
        for profile, layers in (("cpu", -1), ("cpu", 14), ("hybrid", -1),
                                ("auto", 14), ("hybrid", True)):
            with self.subTest(profile=profile, layers=layers), self.assertRaises(ValueError):
                PlacementPlan(profile, layers)

    def test_frozen_plan_and_detached_return_values(self):
        plan = build_plan("hybrid")
        with self.assertRaises(FrozenInstanceError):
            plan.num_gpu = 99
        record, options = plan.record(), plan.ollama_options()
        record["num_gpu"] = options["num_gpu"] = 99
        self.assertEqual(plan.num_gpu, 14)
        self.assertEqual(plan.ollama_options()["num_gpu"], 14)

    def test_plan_hash_is_canonical_and_configuration_specific(self):
        plan = build_plan("hybrid")
        expected = sha256(json.dumps(plan.record(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(plan.sha256, expected)
        self.assertEqual(plan.sha256, build_plan("hybrid", 14).sha256)
        self.assertNotEqual(plan.sha256, build_plan("hybrid", 15).sha256)
        self.assertEqual(len({build_plan(profile).sha256 for profile in ("cpu", "hybrid", "auto")}), 3)

    def test_identity_binds_all_three_caller_verified_digests(self):
        fields = {"model_sha256": "a" * 64, "backend_tree_sha256": "b" * 64,
                  "plan_sha256": build_plan("cpu").sha256}
        identity = artifact_identity(**fields)
        self.assertEqual(identity, artifact_identity(**fields))
        for name in fields:
            with self.subTest(name=name):
                changed = dict(fields, **{name: "c" * 64})
                self.assertNotEqual(identity, artifact_identity(**changed))

    def test_identity_digest_rejects_wrong_types_case_and_noncanonical_text(self):
        fields = {"model_sha256": "a" * 64, "backend_tree_sha256": "b" * 64, "plan_sha256": "c" * 64}
        for name in fields:
            for value in (True, None, b"a" * 64, "a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    artifact_identity(**dict(fields, **{name: value}))


class PlacementObservationTests(unittest.TestCase):
    CUDA = ("load_backend: loaded CPU backend from cpu.dll\n"
            "load_backend: loaded CUDA backend from cuda.dll\n"
            "load_tensors: offloaded 14/37 layers to GPU\n"
            "load_tensors:        CUDA0 model buffer size = 1536.25 MiB\n"
            "llama_kv_cache:        CPU KV buffer size = 64.00 MiB\n")
    NO_DEVICE_PROBE = ('time=x level=DEBUG source=native_probe.go:259 msg="native GPU discovery failed" '
        'error="exit status 1" stderr="ggml_cuda_init: failed to initialize CUDA: '
        'no CUDA-capable device is detected\\r\\nload_backend: loaded CUDA backend from cuda.dll'
        '\\r\\nError: cuInit failed: 100\\n" libDirs="[fixture/cuda_v12]"\n')

    def test_cuda_numeric_offload_is_observation_not_qualification(self):
        result = observe_placement(self.CUDA)
        self.assertEqual(result["status"], "GPU_OFFLOAD_OBSERVED")
        self.assertEqual((result["offloaded_layers"], result["total_layers"]), (14, 37))
        self.assertTrue(result["gpu_offload_observed"])
        self.assertEqual(result["runtimes"], ["CPU", "CUDA"])
        self.assertEqual(result["buffers"][0]["mib"], 1536.25)
        self.assertIs(result["gpu_qualified"], False)
        self.assertIs(result["multi_gpu_qualified"], False)
        self.assertEqual(result["log_sha256"], sha256(self.CUDA.encode()).hexdigest())

    def test_vulkan_and_old_native_prefix(self):
        result = observe_placement("load_backend: loaded Vulkan backend from vk.dll\n"
            "llama_model_load_tensors: offloaded 37/37 layers to GPU\n"
            "llama_context: Vulkan0 compute buffer size = 32.00 MiB\n")
        self.assertTrue(result["gpu_offload_observed"])
        self.assertEqual(result["runtimes"], ["VULKAN"])

    def test_cpu_with_unavailable_gpu_warning_is_not_fatal_or_gpu_success(self):
        result = observe_placement('time=x level=INFO source=types.go msg="inference compute" id=cpu library=cpu\n'
            "warning: no usable GPU found, --gpu-layers option will be ignored\n"
            "load_tensors: CPU model buffer size = 604.15 MiB\n")
        self.assertEqual(result["status"], "CPU_OBSERVED")
        self.assertFalse(result["gpu_offload_observed"])
        self.assertEqual(result["warnings"], ["gpu-unavailable"])

    def test_zero_offloaded_with_cuda_library_is_not_gpu_execution(self):
        result = observe_placement(self.CUDA.replace("14/37", "0/37"))
        self.assertEqual(result["offloaded_layers"], 0)
        self.assertFalse(result["gpu_offload_observed"])

    def test_gpu_discovery_config_and_quoted_example_do_not_prove_offload(self):
        for text in ('msg="discovering available GPUs..." CUDA_VISIBLE_DEVICES=0\n',
                     '"load_tensors: offloaded 14/37 layers to GPU"\n',
                     "load_backend: loaded CUDA backend from cuda.dll\n",
                     "load_tensors: offloaded 14/37 layers to GPU\n"):
            with self.subTest(text=text):
                result = observe_placement(text)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertFalse(result["gpu_offload_observed"])

    def test_errors_after_positive_offload_block_gpu_success(self):
        for error in ('time=x level=ERROR msg="runner failed"', "CUDA error: out of memory",
                      "failed to allocate CUDA buffer", "runner process has terminated"):
            with self.subTest(error=error):
                result = observe_placement(self.CUDA + error)
                self.assertEqual(result["status"], "FAILED")
                self.assertFalse(result["gpu_offload_observed"])

    def test_unavailable_or_fallback_contradicts_positive_offload(self):
        for warning in ("warning: no usable GPU found", "falling back to CPU"):
            with self.subTest(warning=warning):
                result = observe_placement(self.CUDA + warning)
                self.assertIn("offload-unavailable-conflict", result["errors"])
                self.assertFalse(result["gpu_offload_observed"])

    def test_conflicting_runs_are_not_resolved_using_last_line(self):
        result = observe_placement(self.CUDA + "load_tensors: offloaded 0/37 layers to GPU\n")
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["offloaded_layers"])
        self.assertIn("conflicting-offload-counts", result["errors"])

    def test_identical_counter_repetition_is_harmless(self):
        result = observe_placement(self.CUDA + "load_tensors: offloaded 14/37 layers to GPU\n")
        self.assertTrue(result["gpu_offload_observed"])

    def test_invalid_layer_counters_fail_closed(self):
        for counter in ("38/37", "1/0", "1/4097", "-1/37", "True/37", "14.0/37", "14/37trailing"):
            text = self.CUDA.replace("14/37", counter)
            with self.subTest(counter=counter):
                result = observe_placement(text)
                self.assertEqual(result["status"], "FAILED")
                self.assertFalse(result["gpu_offload_observed"])

    def test_log_bound_type_and_unicode_are_strict(self):
        for text in (None, b"CPU", True, "x" * (1024 * 1024 + 1), "\ud800"):
            with self.subTest(kind=type(text)), self.assertRaises(ValueError):
                observe_placement(text)

    def test_structured_offload_needs_actual_gpu_model_buffer(self):
        scheduled = ('time=x level=INFO source=memory.go:123 msg="offload to cuda" '
                     'layers.requested=14 layers.offload=14 layers.model=29\n')
        result = observe_placement(scheduled + "load_backend: loaded CUDA backend from cuda.dll\n")
        self.assertEqual((result["offloaded_layers"], result["total_layers"]), (14, 29))
        self.assertEqual(result["offload_count_source"], "structured")
        self.assertFalse(result["gpu_offload_observed"])
        self.assertEqual(result["status"], "UNKNOWN")
        result = observe_placement(scheduled + "load_tensors: CUDA0 model buffer size = 1200.0 MiB\n")
        self.assertTrue(result["gpu_offload_observed"])
        self.assertFalse(result["gpu_qualified"])

    def test_structured_counts_duplicates_bool_and_trailing_text_fail_closed(self):
        prefix = 'time=x level=INFO source=memory.go:123 msg="offload to cuda" '
        for fields in ("layers.model=29", "layers.model=29 layers.offload=True",
                       "layers.model=29 layers.offload=14 layers.offload=14",
                       "layers.model=29 layers.offload=14oops", "layers.model=29 layers.offload=30"):
            with self.subTest(fields=fields):
                result = observe_placement(prefix + fields)
                self.assertEqual(result["status"], "FAILED")
                self.assertFalse(result["gpu_offload_observed"])

    def test_quoted_structured_records_are_not_runtime_observations(self):
        text = ('example: time=x level=INFO source=memory.go:123 msg="offload to cuda" '
                'layers.offload=14 layers.model=29\n'
                'example: time=x level=INFO source=types.go:50 msg="inference compute" library=cuda\n')
        result = observe_placement(text)
        self.assertIsNone(result["offloaded_layers"])
        self.assertEqual(result["runtimes"], [])
        self.assertEqual(result["status"], "UNKNOWN")

    def test_excessive_native_integer_is_bounded_before_conversion(self):
        text = "load_tensors: offloaded " + "1" * 10000 + "/29 layers to GPU\n"
        result = observe_placement(text)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("offload-count-range", result["errors"])

    def test_masked_cuda_discovery_debug_is_warning_with_real_cpu_buffers(self):
        result = observe_placement(self.NO_DEVICE_PROBE +
            "load_backend: loaded CPU backend from cpu.dll\n"
            "warning: no usable GPU found, --gpu-layers option will be ignored\n"
            "load_tensors: CPU model buffer size = 604.15 MiB\n"
            "llama_kv_cache: CPU KV buffer size = 224.00 MiB\n")
        self.assertEqual(result["status"], "CPU_OBSERVED")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["runtimes"], ["CPU"])
        self.assertIn("gpu-discovery-unavailable", result["warnings"])
        self.assertFalse(result["gpu_offload_observed"])

    def test_unavailable_discovery_alone_is_not_cpu_or_cuda_execution(self):
        result = observe_placement(self.NO_DEVICE_PROBE)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["runtimes"], [])
        self.assertFalse(result["gpu_offload_observed"])

    def test_discovery_exception_does_not_hide_runner_or_error_level_failure(self):
        for text in (self.NO_DEVICE_PROBE.replace("native_probe.go:259", "runner.go:259"),
                     self.NO_DEVICE_PROBE.replace("level=DEBUG", "level=ERROR"),
                     self.NO_DEVICE_PROBE.replace('msg="native GPU discovery failed"', 'msg="runner initialization failed"'),
                     "ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected\n",
                     self.NO_DEVICE_PROBE + "failed to allocate CUDA model buffer\n"):
            with self.subTest(text=text[:90]):
                result = observe_placement(text)
                self.assertEqual(result["status"], "FAILED")
                self.assertIn("runtime-error", result["errors"])
                self.assertFalse(result["gpu_offload_observed"])

    def test_no_device_discovery_still_conflicts_with_positive_gpu_offload(self):
        result = observe_placement(self.NO_DEVICE_PROBE + self.CUDA)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("offload-unavailable-conflict", result["errors"])
        self.assertFalse(result["gpu_offload_observed"])


if __name__ == "__main__":
    unittest.main()

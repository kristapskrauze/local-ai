import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import telemetry


class FakeTrace:
    def __init__(self):
        self.llm = []

    def add_llm(self, record):
        self.llm.append(record)


def ps(*rows):
    return {"ok": True, "models": list(rows)}


class TelemetryEvictionCases(unittest.TestCase):
    def setUp(self):
        self.real_current = telemetry.current
        self.trace = FakeTrace()
        telemetry.current = lambda: self.trace

    def tearDown(self):
        telemetry.current = self.real_current

    def test_load_reason_names_context_runner_swap(self):
        telemetry.record_llm(
            "agent:model",
            "qwen3.8:latest",
            8.0,
            body={"load_duration": 7_000_000_000, "done_reason": "stop"},
            options={"num_ctx": 16384},
            ps_before=ps({
                "model": "qwen3.8:latest",
                "context_length": 32768,
                "size_vram_gb": 17.3,
                "size_gb": 17.3,
            }),
            ps_after=ps({
                "model": "qwen3.8:latest",
                "context_length": 16384,
                "size_vram_gb": 17.3,
                "size_gb": 17.3,
            }),
        )

        rec = self.trace.llm[-1]
        self.assertIn("different context_length", rec["load_reason"])
        self.assertEqual(rec["runner_context_length"], 16384)
        self.assertEqual(rec["runner_device"], "gpu")

    def test_cpu_embedding_runner_is_not_blamed_as_gpu_eviction(self):
        telemetry.record_llm(
            "agent:model",
            "qwen3.8:latest",
            44.0,
            body={"load_duration": 41_000_000_000, "done_reason": "stop"},
            options={"num_ctx": 16384},
            ps_before=ps({
                "model": "embed:latest",
                "context_length": 8192,
                "size_vram_gb": 0.0,
                "size_gb": 1.1,
            }),
            ps_after=ps({
                "model": "qwen3.8:latest",
                "context_length": 16384,
                "size_vram_gb": 17.3,
                "size_gb": 17.3,
            }),
        )

        self.assertIn("only CPU embedding runner", self.trace.llm[-1]["load_reason"])
        self.assertIn("not a GPU-offload eviction", self.trace.llm[-1]["load_reason"])

    def test_embedding_records_cpu_only_num_gpu_zero(self):
        telemetry.record_embed(
            "embed",
            1.5,
            count=4,
            options={"num_gpu": 0},
            ps_after=ps({
                "model": "embed:latest",
                "context_length": 8192,
                "size_vram_gb": 0.0,
                "size_gb": 1.1,
            }),
        )

        rec = self.trace.llm[-1]
        self.assertEqual(rec["embedding_options"]["num_gpu"], 0)
        self.assertEqual(rec["embedding_device"], "cpu")
        self.assertNotIn("embedding_offload_warning", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Offline checks: shadow comparison must never decide the BGE response."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.memory.nas_client import NasMemoryClient
from core.memory.shadow import ShadowRecorder


class MemoryShadowTests(unittest.TestCase):
    def test_shadow_is_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch("core.memory.shadow.ShadowRecorder") as factory:
            client = NasMemoryClient()
        self.assertIsNone(client.shadow)
        self.assertEqual("http://127.0.0.1:8765", client.base_url)
        factory.assert_not_called()

    def test_opt_in_setup_failure_does_not_break_startup(self):
        with patch.dict(os.environ, {"RIO_MEMORY_SHADOW": "1"}, clear=True), patch("core.memory.shadow.ShadowRecorder", side_effect=ValueError()):
            self.assertIsNone(NasMemoryClient().shadow)

    def test_shadow_submission_failure_preserves_bge_selection(self):
        rows = [{"id": "fixture-a", "content": "synthetic fact", "score": 0.9},
                {"id": "fixture-b", "content": "irrelevant fixture", "score": 0.2}]
        with patch.dict(os.environ, {}, clear=True):
            client = NasMemoryClient()
        client.shadow = Mock()
        client.shadow.submit.side_effect = RuntimeError("synthetic shadow failure")
        with patch("core.memory.nas_client.httpx.Client") as http:
            http.return_value.__enter__.return_value.post.return_value.json.return_value = {"results": rows}
            self.assertEqual(rows[:1], client.search("synthetic question"))
        client.shadow.submit.assert_called_once()

    def test_queue_full_drops_comparison_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory, patch("core.memory.shadow.threading.Thread"):
            recorder = ShadowRecorder("http://127.0.0.1:8767", Path(directory) / "shadow.jsonl", capacity=1)
            recorder.submit("one fixture", [], 1, 5, 0.6)
            recorder.submit("another fixture", [], 1, 5, 0.6)
            self.assertEqual(1, recorder.queue.qsize())
            self.assertEqual(1, recorder.dropped)

    def test_private_log_excludes_query_and_memory_content(self):
        with tempfile.TemporaryDirectory() as directory, patch("core.memory.shadow.threading.Thread"):
            path = Path(directory) / "shadow.jsonl"
            recorder = ShadowRecorder("http://127.0.0.1:8767", path)
            recorder.submit("synthetic-query-must-not-log", [{"id": "fixture-id", "content": "synthetic-content-must-not-log", "score": 0.9}], 1, 5, 0.6)
            _, event = recorder.queue.get_nowait()
            recorder._write(event)
            text = path.read_text()
            self.assertNotIn("synthetic-query-must-not-log", text)
            self.assertNotIn("synthetic-content-must-not-log", text)
            self.assertEqual(hashlib.sha256(b"synthetic-query-must-not-log").hexdigest(), json.loads(text)["query_id"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_large_log_rotates_only_its_own_file(self):
        with tempfile.TemporaryDirectory() as directory, patch("core.memory.shadow.threading.Thread"):
            path = Path(directory) / "shadow.jsonl"
            path.write_bytes(b"x" * (10 * 1024 ** 2 + 1))
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            untouched = Path(directory) / "original-memory.json"
            untouched.write_bytes(b"synthetic-preserved-memory\n")
            before = hashlib.sha256(untouched.read_bytes()).hexdigest()
            recorder = ShadowRecorder("http://127.0.0.1:8767", path)
            recorder._write({"status": "fixture"})
            self.assertEqual(original, hashlib.sha256(path.with_suffix(".previous.jsonl").read_bytes()).hexdigest())
            self.assertEqual(before, hashlib.sha256(untouched.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.memory.long_term import LongTermMemoryStore, MemoryStoreError


class LongTermMemorySafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "memory.json"
        self.store = LongTermMemoryStore(self.path)
        self.rows = [
            {"id": "abc001", "content": "first fact"},
            {"id": "abc002", "content": "second fact"},
            {"id": "xyz003", "content": "third fact"},
        ]
        self.path.write_text(json.dumps(self.rows), encoding="utf-8")

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_empty_ids_rejected_with_existing_file_hash_unchanged(self):
        for memory_id in ("", " ", "\t\r\n"):
            with self.subTest(memory_id=memory_id):
                before = self.digest()
                with self.assertRaises(ValueError):
                    self.store.forget(memory_id)
                self.assertEqual(before, self.digest())
                self.assertEqual(self.rows, self.store.all())

    def test_ambiguous_prefix_rejected_with_existing_file_hash_unchanged(self):
        before = self.digest()
        with self.assertRaises(ValueError):
            self.store.forget("abc")
        self.assertEqual(before, self.digest())

    def test_unknown_id_keeps_existing_file_hash_unchanged(self):
        before = self.digest()
        self.assertFalse(self.store.forget("unknown"))
        self.assertEqual(before, self.digest())

    def test_unique_prefix_deletes_only_one_memory(self):
        self.assertTrue(self.store.forget("xyz"))
        self.assertEqual(self.rows[:2], self.store.all())

    def test_exact_id_takes_precedence_over_prefix(self):
        rows = [{"id": "abc", "content": "short"}, *self.rows]
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        self.assertTrue(self.store.forget("abc"))
        self.assertEqual(self.rows, self.store.all())

    def test_invalid_files_block_writes_with_existing_file_hash_unchanged(self):
        payloads = [
            b'[{"id":"old","content":"valuable fact"}',
            b'{"id":"old","content":"wrong root"}',
            b'[{"id":"old","content":"valid"}, null]',
            b'[{"id":"old","content":42}]',
            b'[{"id":"","content":"empty id"}]',
            b'[{"id":"dup","content":"a"},{"id":"dup","content":"b"}]',
            b'\xff\xfeinvalid utf-8',
        ]
        for payload in payloads:
            for operation in (lambda: self.store.remember("new fact"),
                              lambda: self.store.forget("old")):
                with self.subTest(payload=payload, operation=operation):
                    self.path.write_bytes(payload)
                    before = self.digest()
                    with self.assertRaises(MemoryStoreError):
                        operation()
                    self.assertEqual(before, self.digest())
                    self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_read_error_blocks_write_with_existing_file_hash_unchanged(self):
        before = self.digest()
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(MemoryStoreError):
                self.store.remember("new fact")
        self.assertEqual(before, self.digest())

    def test_valid_remember_and_key_update(self):
        memory = self.store.remember("GPU A", key="user:gpu")
        updated = self.store.remember("GPU B", key="user:gpu")
        self.assertEqual(memory["id"], updated["id"])
        self.assertEqual(4, self.store.count())
        self.assertEqual("GPU B", updated["content"])

    def test_missing_store_can_be_created(self):
        new_path = self.path.parent / "new.json"
        store = LongTermMemoryStore(new_path)
        self.assertEqual(0, store.count())
        store.remember("first fact")
        self.assertEqual(1, store.count())


if __name__ == "__main__":
    unittest.main()

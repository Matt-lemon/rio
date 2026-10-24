import hashlib
import json
import multiprocessing
import os
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.memory.long_term import LongTermMemoryStore, MemoryStoreError


def _concurrent_memory_writer(path, prefix, count):
    store = LongTermMemoryStore(Path(path))
    for index in range(count):
        store.remember(f'{prefix}: {index}')


def _crash_before_atomic_replace(path):
    store = LongTermMemoryStore(Path(path))
    with patch('core.memory.long_term.os.replace',
               side_effect=lambda *_: os.kill(os.getpid(), signal.SIGKILL)):
        store.remember('crashing write')


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

    def test_concurrent_process_writes_preserve_every_unique_record(self):
        ctx = multiprocessing.get_context('fork')
        children = [ctx.Process(target=_concurrent_memory_writer,
                                args=(str(self.path), f'writer{n}', 35))
                    for n in range(3)]
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=20)
            if child.is_alive():
                child.terminate()
                child.join()
            self.assertEqual(child.exitcode, 0)
        contents = self.store.all()
        self.assertEqual(3 + 3 * 35, len(contents))
        self.assertEqual(len(contents), len({row['id'] for row in contents}))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_leftover_partial_temp_does_not_corrupt_committed_memory(self):
        original = self.digest()
        partial = self.path.parent / ('.' + self.path.name + '.crashed.tmp')
        partial.write_text('{"incomplete":', encoding='utf-8')
        self.assertEqual(original, self.digest())
        self.store.remember('safe after crash')
        self.assertEqual(4, self.store.count())
        self.assertEqual(json.loads(self.path.read_text())[-1]['content'],
                         'safe after crash')

    def test_sigkill_before_replace_preserves_last_complete_file(self):
        original = self.digest()
        ctx = multiprocessing.get_context('fork')
        child = ctx.Process(target=_crash_before_atomic_replace,
                            args=(str(self.path),))
        child.start()
        child.join(timeout=8)
        if child.is_alive():
            child.terminate()
            child.join()
        self.assertEqual(-signal.SIGKILL, child.exitcode)
        self.assertEqual(original, self.digest())
        self.store.remember('next safe write')
        self.assertEqual(4, self.store.count())

    def test_writes_and_lock_files_use_private_permissions(self):
        self.store.remember('secure')
        lock = self.path.with_name(self.path.name + '.lock')
        self.assertEqual(0o600, os.stat(lock).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import fcntl
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _tokens(text: str) -> set[str]:
    raw = re.findall(
        r"[0-9a-zA-Z가-힣_+.-]+",
        text.lower(),
    )

    result: set[str] = set()

    particles = (
        "에서",
        "에게",
        "으로",
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "의",
        "도",
        "만",
        "로",
    )

    for token in raw:
        result.add(token)

        for particle in particles:
            if (
                token.endswith(particle)
                and len(token) > len(particle) + 1
            ):
                result.add(token[:-len(particle)])
                break

    return result


class MemoryStoreError(RuntimeError):
    """Existing memory data could not be read safely; do not overwrite it."""


class LongTermMemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def _write_lock(self):
        # Persistent adjacent lock: locking the replaced JSON inode would be
        # unsafe because os.replace changes that inode between writers.
        lock = self.path.with_name(self.path.name + '.lock')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(lock, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> list[dict]:
        try:
            data = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MemoryStoreError(f"기억 파일을 읽을 수 없습니다: {self.path}") from exc

        if not isinstance(data, list):
            raise MemoryStoreError(f"기억 파일은 목록이어야 합니다: {self.path}")

        ids = set()
        for item in data:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"].strip()
                    or not isinstance(item.get("content"), str)
                    or item["id"] in ids):
                raise MemoryStoreError(f"잘못된 기억 항목이 있습니다: {self.path}")
            ids.add(item["id"])
        return data

    def _save(self, memories: list[dict]) -> None:
        # A unique 0600 temp file prevents competing processes from writing
        # the same staging inode. Flush file AND parent-directory metadata.
        fd, temp = tempfile.mkstemp(prefix='.' + self.path.name + '.',
                                    suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(memories, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            parent_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def remember(
        self,
        content: str,
        *,
        key: str | None = None,
        importance: float = 0.5,
        source: str = "user",
    ) -> dict:
        content = content.strip()

        if not content:
            raise ValueError("빈 기억은 저장할 수 없습니다.")

        with self._write_lock():
            memories = self._load()
            normalized = _normalize(content)
            normalized_key = _normalize(key) if key else None
            now = _now()

            for memory in memories:
                existing_key = memory.get("key")

                if normalized_key and existing_key == normalized_key:
                    memory["content"] = content
                    memory["importance"] = float(importance)
                    memory["source"] = source
                    memory["updated_at"] = now
                    self._save(memories)
                    return memory

                if _normalize(memory["content"]) == normalized:
                    memory["updated_at"] = now
                    self._save(memories)
                    return memory

            memory = {
                "id": uuid.uuid4().hex[:12],
                "content": content,
                "key": normalized_key,
                "importance": float(importance),
                "source": source,
                "created_at": now,
                "updated_at": now,
            }

            memories.append(memory)
            self._save(memories)
            return memory

    def forget(self, memory_id: str) -> bool:
        memory_id = memory_id.strip()
        if not memory_id:
            raise ValueError("빈 기억 ID로 삭제할 수 없습니다.")
        with self._write_lock():
            memories = self._load()

            matches = [memory for memory in memories if memory["id"] == memory_id]
            if not matches:
                matches = [memory for memory in memories
                           if memory["id"].startswith(memory_id)]
            if not matches:
                return False
            if len(matches) != 1:
                raise ValueError("여러 기억에 일치하는 ID입니다. 더 긴 ID를 사용하세요.")

            kept = [memory for memory in memories if memory["id"] != matches[0]["id"]]
            self._save(kept)
            return True

    def all(self) -> list[dict]:
        memories = self._load()

        return sorted(
            memories,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    def count(self) -> int:
        return len(self._load())

    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[dict]:
        query_norm = _normalize(query)
        query_tokens = _tokens(query)

        if not query_norm:
            return []

        scored: list[tuple[float, dict]] = []

        for memory in self._load():
            content = memory["content"]
            content_norm = _normalize(content)
            content_tokens = _tokens(content)

            overlap = len(query_tokens & content_tokens)

            union = len(query_tokens | content_tokens)
            jaccard = overlap / union if union else 0.0

            similarity = SequenceMatcher(
                None,
                query_norm,
                content_norm,
            ).ratio()

            substring_bonus = 0.0

            if (
                query_norm in content_norm
                or content_norm in query_norm
            ):
                substring_bonus = 1.5

            score = (
                overlap * 2.0
                + jaccard * 2.0
                + similarity
                + substring_bonus
                + float(memory.get("importance", 0.5)) * 0.25
            )

            if overlap == 0 and similarity < 0.28:
                continue

            result = dict(memory)
            result["_score"] = round(score, 4)
            scored.append((score, result))

        scored.sort(
            key=lambda pair: pair[0],
            reverse=True,
        )

        return [
            memory
            for _, memory in scored[:limit]
        ]

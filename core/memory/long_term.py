from __future__ import annotations

import json
import re
import uuid
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


class LongTermMemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []

        try:
            data = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(data, list):
            return []

        return [
            item
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("content"), str)
        ]

    def _save(self, memories: list[dict]) -> None:
        temp = self.path.with_suffix(".tmp")

        temp.write_text(
            json.dumps(
                memories,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp.replace(self.path)

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
        memories = self._load()

        kept = [
            memory
            for memory in memories
            if not memory["id"].startswith(memory_id)
        ]

        if len(kept) == len(memories):
            return False

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

from __future__ import annotations

import re
from pathlib import Path

from .long_term import LongTermMemoryStore


class MemoryManager:
    def __init__(self, path: Path):
        self.store = LongTermMemoryStore(path)

    def remember(
        self,
        content: str,
        *,
        key: str | None = None,
        importance: float = 0.5,
        source: str = "user",
    ) -> dict:
        return self.store.remember(
            content,
            key=key,
            importance=importance,
            source=source,
        )

    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[dict]:
        return self.store.search(query, limit=limit)

    def all(self) -> list[dict]:
        return self.store.all()

    def count(self) -> int:
        return self.store.count()

    def forget(self, memory_id: str) -> bool:
        return self.store.forget(memory_id)

    def consider_user_message(
        self,
        text: str,
    ) -> dict | None:
        text = text.strip()

        if not text:
            return None

        # 질문을 사실로 잘못 기억하는 것 방지
        question_words = (
            "뭐",
            "무엇",
            "어디",
            "언제",
            "누구",
            "몇",
            "왜",
            "어떻게",
        )

        # 명시적인 "기억해" 요청
        memory_words = (
            "기억해줘",
            "기억해 줘",
            "기억해둬",
            "기억해 둬",
            "기억해",
        )

        for word in memory_words:
            if word in text:
                content = text.replace(word, "").strip(" .,:：!?")

                if content:
                    return self.remember(
                        content,
                        importance=0.9,
                        source="explicit-user",
                    )

        # 안정적인 사용자 속성:
        # "내 메인 GPU는 9070 XT야."
        match = re.match(
            r"^\s*내\s+(.+?)(은|는|이|가)\s+(.+?)\s*[.!]?\s*$",
            text,
        )

        if not match:
            return None

        subject = match.group(1).strip()
        value = match.group(3).strip()

        if (
            "?" in text
            or any(word in value for word in question_words)
        ):
            return None

        if len(subject) > 80 or len(value) > 300:
            return None

        return self.remember(
            text,
            key=f"user:{subject}",
            importance=0.75,
            source="auto-user-fact",
        )

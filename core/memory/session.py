from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(data, list):
            return []

        result = []

        for item in data:
            if not isinstance(item, dict):
                continue

            role = item.get("role")
            content = item.get("content")

            if role not in {"user", "assistant"}:
                continue

            if not isinstance(content, str):
                continue

            result.append({
                "role": role,
                "content": content,
            })

        return result

    def save(self, messages: list[dict[str, str]]) -> None:
        temp = self.path.with_suffix(".tmp")

        temp.write_text(
            json.dumps(
                messages,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp.replace(self.path)

    def clear(self) -> None:
        self.save([])

    def new_session(self) -> Path | None:
        messages = self.load()

        if not messages:
            self.clear()
            return None

        archive_dir = self.path.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"session_{stamp}.json"

        archive_path.write_text(
            json.dumps(
                messages,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        self.clear()

        return archive_path

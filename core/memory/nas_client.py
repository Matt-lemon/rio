from __future__ import annotations

import os
from typing import Any

import httpx


class NasMemoryClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "MEMORY_API_URL",
            "http://127.0.0.1:8765",
        ).rstrip("/")

        self.timeout = float(
            os.getenv("MEMORY_API_TIMEOUT", "8")
        )

        self.min_score = float(
            os.getenv("MEMORY_MIN_SCORE", "0.60")
        )

        self.default_limit = int(
            os.getenv("MEMORY_SEARCH_LIMIT", "5")
        )

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/health"
            )
            response.raise_for_status()
            data = response.json()

        return data if isinstance(data, dict) else {}

    def remember(
        self,
        content: str,
        *,
        key: str | None = None,
        importance: float = 0.5,
        source: str = "rio",
    ) -> dict[str, Any]:
        payload = {
            "content": content,
            "key": key,
            "importance": importance,
            "source": source,
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/memory",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        return data if isinstance(data, dict) else {}

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        if limit is None:
            limit = self.default_limit

        if min_score is None:
            min_score = self.min_score

        payload = {
            "query": query,
            "limit": limit,
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/search",
                json=payload,
            )
            response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            return []

        results = data.get("results", [])
        if not isinstance(results, list):
            return []

        filtered: list[dict[str, Any]] = []

        for item in results:
            if not isinstance(item, dict):
                continue

            raw_score = item.get("score", item.get("_score", 0.0))

            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue

            if score < min_score:
                continue

            normalized = dict(item)
            normalized["score"] = score
            filtered.append(normalized)

        return filtered

    def memories(
        self,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/memories",
                params={"limit": limit},
            )
            response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            return []

        results = data.get("results", [])
        return results if isinstance(results, list) else []

    def forget(
        self,
        memory_id: str,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.delete(
                f"{self.base_url}/memory/{memory_id}"
            )
            response.raise_for_status()
            data = response.json()

        return data if isinstance(data, dict) else {}

    def safe_search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return self.search(
                query,
                limit=limit,
            )
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
        ):
            return []

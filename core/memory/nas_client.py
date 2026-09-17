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
            return response.json()

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
            return response.json()

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

        results = data.get("results", [])

        return [
            item
            for item in results
            if float(item.get("score", 0.0)) >= min_score
        ]

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

        return response.json().get("results", [])

    def forget(
        self,
        memory_id: str,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.delete(
                f"{self.base_url}/memory/{memory_id}"
            )
            response.raise_for_status()
            return response.json()

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
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.HTTPError,
        ):
            return []

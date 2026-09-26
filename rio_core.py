from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
IDENTITY_PATH = ROOT / "config" / "identity.yaml"

load_dotenv(ROOT / ".env")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "local-model")
LLM_API_KEY = os.getenv("LLM_API_KEY", "not-needed")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))

LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.8"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.95"))
LLM_TOP_K = int(os.getenv("LLM_TOP_K", "64"))
LLM_MIN_P = float(os.getenv("LLM_MIN_P", "0.05"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))
LLM_REPEAT_PENALTY = float(os.getenv("LLM_REPEAT_PENALTY", "1.12"))
LLM_REPEAT_LAST_N = int(os.getenv("LLM_REPEAT_LAST_N", "128"))
HISTORY_LIMIT = max(0, int(os.getenv("RIO_HISTORY_LIMIT", "20")))
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none")

def load_identity() -> dict[str, Any]:
    if not IDENTITY_PATH.exists():
        return {}

    with IDENTITY_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data if isinstance(data, dict) else {}


def build_system_prompt(identity: dict[str, Any]) -> str:
    """
    Build the shared prompt from persistent identity configuration.

    Memory, routing, transient state and other orchestration concerns remain
    outside this runtime. The prompt keeps the stable identity/personality
    fields that are explicitly configured in identity.yaml.
    """
    identity_cfg = identity.get("identity", {})
    personality = identity.get("personality", {})

    name = str(identity_cfg.get("name", "")).strip()
    role = str(identity_cfg.get("role", "")).strip()
    user_address = str(identity_cfg.get("user_address", "")).strip()
    core = identity_cfg.get("core", [])
    persona = str(personality.get("persona", "")).strip()
    tone = personality.get("tone", [])
    interaction = personality.get("interaction", [])
    avoid = personality.get("avoid", [])

    parts: list[str] = []

    if name:
        parts.append(f"너의 이름은 {name}다.")

    if role:
        parts.append(f"역할: {role}")

    if user_address:
        parts.append(f"사용자 호칭: {user_address}")

    if core:
        parts.append("")
        parts.append("### 정체성 핵심")
        for item in core:
            parts.append(f"- {item}")

    if persona:
        parts.append("")
        parts.append(persona)

    if tone:
        parts.append("")
        parts.append("### 말투와 표현")
        for item in tone:
            parts.append(f"- {item}")

    if interaction:
        parts.append("")
        parts.append("### 대화와 작업 방식")
        for item in interaction:
            parts.append(f"- {item}")

    if avoid:
        parts.append("")
        parts.append("### 피할 표현")
        for item in avoid:
            parts.append(f"- {item}")

    return "\n".join(parts).strip()


def api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }


def completion_payload(
    messages: list[dict[str, str]], *, max_tokens: int | None = None
) -> dict[str, Any]:
    return {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "top_k": LLM_TOP_K,
        "min_p": LLM_MIN_P,
        "repeat_penalty": LLM_REPEAT_PENALTY,
        "repeat_last_n": LLM_REPEAT_LAST_N,
        "max_tokens": LLM_MAX_TOKENS if max_tokens is None else max_tokens,
        "stream": True,
        "reasoning_effort": LLM_REASONING_EFFORT,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def stream_text(lines: Iterable[str | bytes]) -> Iterator[str]:
    """Read OpenAI-compatible SSE content deltas from either HTTP client."""
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = event.get("choices") or []
        if choices:
            content = (choices[0].get("delta") or {}).get("content")
            if content:
                yield content


def build_messages(
    system_prompt: str,
    history: list[dict[str, str]],
    user_text: str,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    if system_prompt:
        messages.append({
            "role": "system",
            "content": system_prompt,
        })

    if HISTORY_LIMIT > 0:
        messages.extend(history[-HISTORY_LIMIT:])

    messages.append({
        "role": "user",
        "content": user_text,
    })

    return messages


def trim_history(history: list[dict[str, str]]) -> None:
    if HISTORY_LIMIT <= 0:
        history.clear()
        return

    if len(history) > HISTORY_LIMIT:
        del history[:-HISTORY_LIMIT]

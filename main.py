from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
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
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))


def load_identity() -> dict:
    if not IDENTITY_PATH.exists():
        return {}

    with IDENTITY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_system_prompt(identity: dict) -> str:
    """
    최소 프롬프트 빌더.

    여기서는 기존 프로젝트의:
    - memory
    - routing
    - technical/chat mode
    - hard-coded behavior rules
    - tone/interaction/avoid 보정
    을 전혀 넣지 않는다.

    identity.yaml에서 아래 네 항목만 사용한다:
    - identity.name
    - identity.role
    - identity.user_address
    - personality.persona
    """
    identity_cfg = identity.get("identity", {})
    personality = identity.get("personality", {})

    name = str(identity_cfg.get("name", "")).strip()
    role = str(identity_cfg.get("role", "")).strip()
    user_address = str(identity_cfg.get("user_address", "")).strip()
    persona = str(personality.get("persona", "")).strip()
    tone = personality.get("tone", [])
    parts: list[str] = []
    interaction = personality.get("interaction", [])
    if name:
        parts.append(f"너의 이름은 {name}다.")

    if role:
        parts.append(f"역할: {role}")

    if user_address:
        parts.append(f"사용자 호칭: {user_address}")

    
        
    if persona:
        if parts:
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

    return "\n".join(parts).strip()


def api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }


def get_loaded_models() -> list[dict]:
    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            response = client.get(
                f"{LLM_BASE_URL}/models",
                headers=api_headers(),
            )
            response.raise_for_status()

        data = response.json()
        models = data.get("data", [])

        return models if isinstance(models, list) else []

    except Exception:
        return []


def stream_completion(messages: list[dict[str, str]]):
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "top_k": LLM_TOP_K,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": True,
    }

    url = f"{LLM_BASE_URL}/chat/completions"

    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        with client.stream(
            "POST",
            url,
            headers=api_headers(),
            json=payload,
        ) as response:
            if response.is_error:
                response.read()
                response.raise_for_status()

            for line in response.iter_lines():
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
                if not choices:
                    continue

                delta = choices[0].get("delta") or {}
                text = delta.get("content")

                if text:
                    yield text


def print_status(system_prompt: str) -> None:
    models = get_loaded_models()

    print()
    print("=== Rio Clean Runtime ===")
    print(f"Server       : {LLM_BASE_URL}")
    print(f"Request model: {LLM_MODEL}")

    if models:
        print("Loaded model :")
        for model in models:
            model_id = model.get("id", "(unknown)")
            print(f"  - {model_id}")
    else:
        print("Loaded model : 확인 실패")

    print(
        f"Sampling     : temp={LLM_TEMPERATURE}, "
        f"top_p={LLM_TOP_P}, "
        f"top_k={LLM_TOP_K}, "
        f"max_tokens={LLM_MAX_TOKENS}"
    )
    print(f"Identity     : {IDENTITY_PATH}")
    print(f"System chars : {len(system_prompt)}")
    print("=========================")
    print()


def main() -> None:
    identity = load_identity()
    system_prompt = build_system_prompt(identity)

    history: list[dict[str, str]] = []

    if system_prompt:
        history.append({
            "role": "system",
            "content": system_prompt,
        })

    print_status(system_prompt)
    print("명령: /status /clear /system /exit")
    print()

    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_text:
            continue

        command = user_text.lower()

        if command in {"/exit", "/quit"}:
            break

        if command == "/status":
            print_status(system_prompt)
            continue

        if command == "/system":
            print()
            print("=== SYSTEM PROMPT ===")
            print(system_prompt or "(empty)")
            print("=====================")
            print()
            continue

        if command == "/clear":
            history = []

            if system_prompt:
                history.append({
                    "role": "system",
                    "content": system_prompt,
                })

            print("현재 프로세스의 대화 문맥을 초기화했어.")
            print()
            continue

        history.append({
            "role": "user",
            "content": user_text,
        })

        print("Rio: ", end="", flush=True)

        chunks: list[str] = []

        try:
            for chunk in stream_completion(history):
                chunks.append(chunk)
                print(chunk, end="", flush=True)

            print()
            print()

        except httpx.ConnectError:
            print()
            print(f"[연결 실패] {LLM_BASE_URL}")
            print()
            history.pop()
            continue

        except httpx.HTTPStatusError as exc:
            print()
            print(
                f"[LLM 서버 오류] "
                f"{exc.response.status_code}: "
                f"{exc.response.text}"
            )
            print()
            history.pop()
            continue

        except httpx.HTTPError as exc:
            print()
            print(f"[통신 오류] {exc}")
            print()
            history.pop()
            continue

        answer = "".join(chunks).strip()

        if not answer:
            history.pop()
            continue

        history.append({
            "role": "assistant",
            "content": answer,
        })


if __name__ == "__main__":
    main()

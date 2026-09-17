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


def load_identity() -> dict:
    with IDENTITY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_system_prompt(identity: dict) -> str:
    i = identity.get("identity", {})
    p = identity.get("personality", {})
    r = identity.get("response", {})

    lines: list[str] = [
        f"너의 이름은 {i.get('name', 'Assistant')}이다.",
        f"역할: {i.get('role', '로컬 AI 비서')}",
        f"사용자 호칭: {i.get('user_address', '사용자')}",
        "",
        "[핵심 정체성]",
    ]

    for item in i.get("core", []):
        lines.append(f"- {item}")

    lines.append("")
    lines.append("[성격/말투]")
    for item in p.get("tone", []):
        lines.append(f"- {item}")

    lines.append("")
    lines.append("[상호작용 규칙]")
    for item in p.get("interaction", []):
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            f"주 언어: {r.get('primary_language', 'ko-KR')}",
            "답변은 기본적으로 간결하게 하되 필요한 경우 충분히 설명한다.",
            "작업 중에도 캐릭터를 유지하되 정확성을 최우선으로 한다.",
        ]
    )

    return "\n".join(lines)


def stream_completion(messages: list[dict[str, str]]):
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.8,
        "top_p": 0.9,
        "stream": True,
    }

    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        with client.stream("POST", url, headers=headers, json=payload) as response:
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


def main() -> None:
    identity = load_identity()
    system_prompt = build_system_prompt(identity)
    assistant_name = identity.get("identity", {}).get("name", "Assistant")

    history: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]

    print(f"{assistant_name} Phase 1")
    print("명령: /clear = 대화 초기화, /exit = 종료\n")

    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료")
            break

        if not user_text:
            continue

        if user_text.lower() in {"/exit", "/quit"}:
            break

        if user_text.lower() == "/clear":
            history = [{"role": "system", "content": system_prompt}]
            print("대화 기록을 초기화했어.\n")
            continue

        history.append({"role": "user", "content": user_text})

        print(f"{assistant_name}: ", end="", flush=True)
        chunks: list[str] = []

        try:
            for chunk in stream_completion(history):
                chunks.append(chunk)
                print(chunk, end="", flush=True)
            print("\n")
        except httpx.ConnectError:
            print(
                "\n[연결 실패] 로컬 LLM 서버에 접속할 수 없어. "
                f"현재 주소: {LLM_BASE_URL}\n"
            )
            history.pop()
            continue
        except httpx.HTTPStatusError as exc:
            print(f"\n[LLM 서버 오류] {exc.response.status_code}: {exc.response.text}\n")
            history.pop()
            continue
        except httpx.HTTPError as exc:
            print(f"\n[통신 오류] {exc}\n")
            history.pop()
            continue

        answer = "".join(chunks).strip()
        if answer:
            history.append({"role": "assistant", "content": answer})
        else:
            history.pop()

        # Phase 1에서는 컨텍스트 폭주를 막기 위해 최근 대화만 유지한다.
        if len(history) > 21:
            history = [history[0], *history[-20:]]


if __name__ == "__main__":
    main()

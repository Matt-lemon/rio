from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from core.memory.session import SessionStore

ROOT = Path(__file__).resolve().parent
IDENTITY_PATH = ROOT / "config" / "identity.yaml"

load_dotenv(ROOT / ".env")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "local-model")
LLM_API_KEY = os.getenv("LLM_API_KEY", "not-needed")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
SESSION_CONTEXT_MESSAGES = int(os.getenv("SESSION_CONTEXT_MESSAGES", "20"))
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none")


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

    lines.append("")
    lines.append("[피해야 할 말투]")
    for item in p.get("avoid", []):
        lines.append(f"- {item}")

    lines.append("")
    lines.append("[말투 예시]")
    for example in p.get("examples", []):
        user = example.get("user", "")
        assistant = example.get("assistant", "")
        lines.append(f"사용자: {user}")
        lines.append(f"리오: {assistant}")

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
        "temperature": 0.6,
        "top_p": 0.85,
        "reasoning_effort": LLM_REASONING_EFFORT,
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

    session_path = ROOT / "data" / "sessions" / "current.json"
    store = SessionStore(session_path)

    session_messages = store.load()

    history: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *session_messages[-SESSION_CONTEXT_MESSAGES:],
    ]

    print(f"{assistant_name} Phase 2")
    print("명령: /clear /new /status /exit")
    print()

    if session_messages:
        restored = min(
            len(session_messages),
            SESSION_CONTEXT_MESSAGES,
        )

        print(
            f"이전 세션 복구: {len(session_messages)}개 메시지 "
            f"(최근 {restored}개 사용)"
        )
    else:
        print("저장된 세션 없음")

    print()

    while True:
        try:
            user_text = input("You: ").strip()

        except (EOFError, KeyboardInterrupt):
            print()
            print("종료")
            break

        if not user_text:
            continue

        command = user_text.lower()

        if command in {"/exit", "/quit"}:
            break

        if command == "/clear":
            session_messages = []
            store.clear()

            history = [
                {"role": "system", "content": system_prompt}
            ]

            print("현재 대화 문맥을 초기화했어.")
            print()
            continue

        if command == "/new":
            archived = store.new_session()

            session_messages = []

            history = [
                {"role": "system", "content": system_prompt}
            ]

            if archived:
                print(f"이전 세션 보관: {archived}")

            print("새 세션을 시작했어.")
            print()
            continue

        if command == "/status":
            reasoning = globals().get(
                "LLM_REASONING_EFFORT",
                "server-default",
            )

            print()
            print("=== Rio Status ===")
            print(f"LLM       : {LLM_MODEL}")
            print(f"Server    : {LLM_BASE_URL}")
            print(f"Session   : {session_path}")
            print(f"Saved     : {len(session_messages)} messages")
            print(
                f"Context   : "
                f"{min(len(session_messages), SESSION_CONTEXT_MESSAGES)}"
                f"/{SESSION_CONTEXT_MESSAGES}"
            )
            print(f"Reasoning : {reasoning}")
            print("==================")
            print()
            continue

        user_message = {
            "role": "user",
            "content": user_text,
        }

        history.append(user_message)

        print(f"{assistant_name}: ", end="", flush=True)

        chunks: list[str] = []

        try:
            for chunk in stream_completion(history):
                chunks.append(chunk)
                print(chunk, end="", flush=True)

            print()
            print()

        except httpx.ConnectError:
            print()
            print(
                "[연결 실패] 로컬 LLM 서버에 접속할 수 없어. "
                f"현재 주소: {LLM_BASE_URL}"
            )
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

        assistant_message = {
            "role": "assistant",
            "content": answer,
        }

        history.append(assistant_message)

        session_messages.extend([
            user_message,
            assistant_message,
        ])

        store.save(session_messages)

        history = [
            {"role": "system", "content": system_prompt},
            *session_messages[-SESSION_CONTEXT_MESSAGES:],
        ]


if __name__ == "__main__":
    main()

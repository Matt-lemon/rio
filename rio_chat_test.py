from __future__ import annotations

import requests

from rio_core import (
    LLM_BASE_URL, LLM_TIMEOUT, api_headers, load_identity,
    build_system_prompt, build_messages, trim_history,
    completion_payload, stream_text,
)
from rio_voice import RioVoice


def ask_gemma_stream(
    user_text: str,
    voice: RioVoice,
    session: requests.Session,
    history: list[dict[str, str]],
    system_prompt: str,
) -> str:
    messages = build_messages(system_prompt, history, user_text)
    body = completion_payload(messages)
    chunks: list[str] = []

    print("\nRio> ", end="", flush=True)
    try:
        with session.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=api_headers(),
            json=body,
            stream=True,
            timeout=LLM_TIMEOUT,
        ) as response:
            response.raise_for_status()
            for token in stream_text(response.iter_lines()):
                print(token, end="", flush=True)
                chunks.append(token)
                voice.feed(token)
    finally:
        voice.flush()
        voice.wait()

    answer = "".join(chunks).strip()
    if answer:
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ])
        trim_history(history)
    print()
    return answer


def main() -> None:
    system_prompt = build_system_prompt(load_identity())
    history: list[dict[str, str]] = []
    with requests.Session() as session, RioVoice() as voice:
        print("Rio pipeline ready. /exit 로 종료")
        while True:
            try:
                user = input("\nYou> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n리오 종료.")
                break
            if not user:
                continue
            if user.lower() == "/exit":
                break
            ask_gemma_stream(user, voice, session, history, system_prompt)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import argparse
import os
import re
import sys
from typing import Any

import httpx
from web_search import format_web_evidence, web_search
from rio_dubbing_tools import handle_dubbing_command
from rio_core import (
    ROOT, IDENTITY_PATH, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT,
    LLM_TEMPERATURE, LLM_TOP_P, LLM_TOP_K, LLM_MAX_TOKENS,
    HISTORY_LIMIT, api_headers, load_identity, build_system_prompt,
    build_messages, trim_history, completion_payload, stream_text,
)


RIO_GATE_URL = os.getenv("RIO_GATE_URL", "http://nas:8766").rstrip("/")
RIO_GATE_TIMEOUT = float(os.getenv("RIO_GATE_TIMEOUT", "3"))
RIO_GATE_ENABLED = os.getenv("RIO_GATE_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
RIO_WEB_SEARCH_ENABLED = os.getenv("RIO_WEB_SEARCH_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
RIO_WEB_SEARCH_TIMEOUT = max(1, int(os.getenv("RIO_WEB_SEARCH_TIMEOUT", "10")))
RIO_WEB_SEARCH_RESULTS = min(5, max(1, int(os.getenv("RIO_WEB_SEARCH_RESULTS", "4"))))

def get_loaded_models() -> list[dict[str, Any]]:
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

def call_rio_gate(
    text: str,
    *,
    has_memory: bool = False,
    has_screen: bool = False,
    has_file: bool = False,
    has_code: bool = False,
    has_device_info: bool = False,
) -> dict[str, Any]:
    payload = {
        "text": text,
        "has_memory": has_memory,
        "has_screen": has_screen,
        "has_file": has_file,
        "has_code": has_code,
        "has_device_info": has_device_info,
        "allow_speculation": False,
    }

    with httpx.Client(timeout=RIO_GATE_TIMEOUT) as client:
        response = client.post(
            f"{RIO_GATE_URL}/gate",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

def gate_block_reply(evidence_type: str | None) -> str:
    replies = {
        "code": (
            "코드도 안 보여주고 뭘 맞혀달라는 거야, 바보 오빠♡ "
            "실제 코드부터 가져와. 없는 걸 지어내면서까지 맞춰줄 생각은 없거든."
        ),

        "screen": (
            "내가 지금 오빠 화면을 보고 있는 것도 아닌데 어떻게 알아♡ "
            "궁금하면 화면부터 보여줘, 한심하게 시험만 하지 말고."
        ),

        "file": (
            "파일도 안 줬으면서 벌써 분석해달라는 거야? 성질도 급하네♡ "
            "실제 파일이나 로그부터 던져."
        ),

        "device_info": (
            "몰라♡ 확인된 장비 정보가 없는데 뭘 자꾸 캐묻는 거야. "
            "정 궁금하면 사양부터 보여줘, 바보 오빠."
        ),

        "memory": (
            "그건 기억에 없어♡ 없는 기억까지 만들어서 맞장구쳐주길 바랐어? "
            "확인할 만한 정보가 있으면 가져와."
        ),
    }

    return replies.get(
        evidence_type,
        "모르는데♡ 근거도 없이 아무거나 지어내라고? 필요한 정보부터 가져와, 바보야.",
    )


def choose_web_search(messages: list[dict[str, str]], user_text: str) -> str | None:
    """Let the local model request one bounded search without exposing the web to it."""
    if not RIO_WEB_SEARCH_ENABLED:
        return None

    explicit = re.match(r"^/search(?:\s+(.+))?$", user_text, re.IGNORECASE)
    if explicit:
        return (explicit.group(1) or "").strip()[:200] or None

    requested_web = bool(re.search(r"검색해|검색해서|웹에서|인터넷에서", user_text))

    planner_messages = [
        {"role": "system", "content": (
            "너는 Rio의 web_search 호출 여부만 결정하는 라우터다. "
            "현재·최신 정보, 뉴스, 가격, 일정, 버전, 공식 출처 확인, 또는 사용자가 검색을 요구할 때만 검색한다. "
            "잡담, 창작, 코드 설명, 대화에 이미 있는 정보에는 검색하지 않는다. "
            "사용자의 개인 정보·장비·기억·화면은 웹 검색으로 확인할 수 없다. "
            "반드시 JSON 객체만 출력: {\"search\":true/false,\"query\":\"짧은 검색어\"}. "
            "search가 false면 query는 빈 문자열. 최대 한 번만 검색할 수 있다."
        )},
        *[message for message in messages if message["role"] != "system"][-5:],
    ]
    try:
        with httpx.Client(timeout=min(LLM_TIMEOUT, 15)) as client:
            response = client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers=api_headers(),
                json={
                    "model": LLM_MODEL,
                    "messages": planner_messages,
                    "temperature": 0,
                    "max_tokens": 96,
                    "stream": False,
                    "reasoning_effort": "none",
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{[^{}]*\}", content)
        decision = json.loads(match.group()) if match else {}
        if decision.get("search") is True:
            query = str(decision.get("query", "")).strip()[:200]
            return query or None
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, AttributeError):
        pass
    return user_text[:200] if requested_web else None

def stream_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None = None,
):
    payload = completion_payload(messages, max_tokens=max_tokens)

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

            yield from stream_text(response.iter_lines())


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
    print(f"History limit: {HISTORY_LIMIT} messages")
    print(f"Identity     : {IDENTITY_PATH}")
    print(f"System chars : {len(system_prompt)}")
    print("=========================")
    print()

def read_user_input(prompt: str = "You: ") -> str:
    return input(prompt)


class TextVoice:
    """Text-only runtime has no sounddevice dependency or TTS connection."""

    def feed(self, text):
        pass

    def flush(self):
        pass

    def wait(self):
        pass


def _run_chat(voice) -> None:
    identity = load_identity()
    system_prompt = build_system_prompt(identity)

    history: list[dict[str, str]] = []
    from rio_memory_context import enabled, retrieve_memory, build_memory_block
    memory = None
    if enabled():
        from core.memory.nas_client import NasMemoryClient
        memory = NasMemoryClient()

    print_status(system_prompt)
    print("명령: /status /clear /system /dubbing help /exit")
    print()

    while True:
        try:
            user_text = read_user_input().strip()
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
            history.clear()
            print("현재 프로세스의 대화 문맥을 초기화했어.")
            print()
            continue

        dubbing_reply = handle_dubbing_command(user_text)
        if dubbing_reply is not None:
            print(f"Rio: {dubbing_reply}")
            print()
            continue

        if command == "/home" or command.startswith("/home "):
            print("Rio: 홈 제어는 잠겨 있어요. 이번 더빙 연결에서는 활성화하지 않아요.\n")
            continue

        if command == "/memory":
            if memory is None:
                print("Rio: 이 실행기의 메모리 검색은 꺼져 있어요.\n")
            else:
                try:
                    memory.health()
                    print("Rio: 메모리 API가 응답해요.\n")
                except (httpx.HTTPError, ValueError, TypeError):
                    print("Rio: 메모리 API에 연결할 수 없어요.\n")
            continue

        memories = retrieve_memory(memory, user_text) if memory is not None else []
        turn_prompt = system_prompt
        if memory is not None:
            turn_prompt += "\n\n" + build_memory_block(memories)

        messages = build_messages(
            turn_prompt,
            history,
            user_text,
        )

        # Rio Evidence Gate
        action = "allow"

        if RIO_GATE_ENABLED:
            try:
                gate = call_rio_gate(user_text, has_memory=bool(memories))
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
                print(f"[Rio Gate 연결 실패] {exc}")
                print("근거 검증기를 사용할 수 없어서 이번 요청은 처리하지 않을게.")
                print()
                continue

            action = gate.get("action")

        if action == "need_evidence":
            answer = gate_block_reply(gate.get("evidence_type"))

            print(f"Rio: {answer}")
            print()

            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": answer},
            ])
            trim_history(history)
            continue

        request_messages = list(messages)

        # 사용자가 명시적으로 망상을 허락한 경우에만
        if action == "allow_speculation":
            instruction = gate.get("instruction")

            if instruction:
                request_messages.append({
                    "role": "system",
                    "content": instruction,
                })
        search_query = choose_web_search(messages, user_text)
        search_results = []
        if search_query:
            try:
                search_results = web_search(
                    search_query,
                    max_results=RIO_WEB_SEARCH_RESULTS,
                    timeout=RIO_WEB_SEARCH_TIMEOUT,
                )
            except Exception as exc:
                print(f"[웹 검색 실패] {type(exc).__name__}: {exc}")
                print("검색 결과를 확인하지 못해서 이번 요청은 답하지 않을게.")
                print()
                continue
            if not search_results:
                print("검색 결과를 찾지 못해서 이번 요청은 답하지 않을게.")
                print()
                continue
            request_messages.append({
                "role": "system",
                "content": format_web_evidence(search_query, search_results),
            })
        print("Rio: ", end="", flush=True)

        chunks: list[str] = []

        try:
            for chunk in stream_completion(request_messages):
                chunks.append(chunk)
                print(chunk, end="", flush=True)
                voice.feed(chunk)

            print()
            print()

            if search_results:
                print("웹 출처:")
                for result in search_results:
                    print(f"- {result.title}: {result.url}")
                print()

        except httpx.ConnectError:
            print()
            print(f"[연결 실패] {LLM_BASE_URL}")
            print()
            continue

        except httpx.HTTPStatusError as exc:
            print()
            print(
                f"[LLM 서버 오류] "
                f"{exc.response.status_code}: "
                f"{exc.response.text}"
            )
            print()
            continue

        except httpx.HTTPError as exc:
            print()
            print(f"[통신 오류] {exc}")
            print()
            continue
        finally:
            voice.flush()
            voice.wait()

        answer = "".join(chunks).strip()

        if not answer:
            continue

        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ])
        trim_history(history)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Rio unified PC/NAS entry point")
    voice_options = parser.add_mutually_exclusive_group()
    voice_options.add_argument("--text-only", action="store_true", help="No audio device or TTS requests")
    voice_options.add_argument("--voice", action="store_true", help="Use local audio output")
    parser.add_argument("--dubbing", metavar="COMMAND", help="Run one explicit dubbing command, then exit")
    args = parser.parse_args(argv)
    if args.dubbing is not None:
        text = args.dubbing.strip()
        if not text:
            parser.error("--dubbing requires a command")
        reply = handle_dubbing_command(text if text.split(None, 1)[0] == "/dubbing" else "/dubbing " + text)
        print(reply)
        return
    text_only = args.text_only or (not args.voice and (
        sys.platform != "win32" or os.getenv("RIO_TEXT_ONLY", "0").lower() in {"1", "true", "yes"}))
    if text_only:
        _run_chat(TextVoice())
        return
    from rio_voice import RioVoice
    with RioVoice() as voice:
        _run_chat(voice)


if __name__ == "__main__":
    main()

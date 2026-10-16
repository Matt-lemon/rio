from __future__ import annotations

import json
import argparse
import os
import re
import sys
from typing import Any

import httpx
from web_search import format_web_evidence, web_search
from core.current_knowledge import VocabularyReferences, runtime_context, suggested_search, refresh_requested, search_forbidden, vocabulary_term, VOCAB, vocabulary_sources
from rio_dubbing_tools import handle_dubbing_command
from core.home.smartthings_pc import handle_pc_command
from core.home.main_adapter import handle_home_command
from core.spotify import handle_spotify_command
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
RIO_CONTEXT_TOOLS_ENABLED = os.getenv('RIO_CONTEXT_TOOLS_ENABLED','true').lower() in {'1','true','yes','on'}

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

    if search_forbidden(user_text):
        return None

    public_query = suggested_search(user_text)
    if public_query:
        return public_query

    requested_web = bool(re.search(r"검색해|검색해서|웹에서|인터넷에서", user_text))

    planner_messages = [
        {"role": "system", "content": (
            "너는 Rio의 web_search 호출 여부만 결정하는 라우터다. "
            "현재·최신 정보, 뉴스, 가격, 일정, 버전, 공식 출처 확인, 또는 사용자가 검색을 요구할 때만 검색한다. "
            "낯선 신조어·유행어·밈의 뜻이나 사용 맥락이 불확실할 때도 짧은 어휘 검색어로 확인한다. "
            "이미 제공된 최근 어휘 참고 자료가 충분하면 중복 검색하지 않는다. "
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


def print_status(system_prompt: str, probe_models: bool = True) -> None:
    models = get_loaded_models() if probe_models else []

    print()
    print("=== Rio Clean Runtime ===")
    print(f"Server       : {LLM_BASE_URL}")
    print(f"Request model: {LLM_MODEL}")

    if models:
        print("Loaded model :")
        for model in models:
            model_id = model.get("id", "(unknown)")
            print(f"  - {model_id}")
    elif probe_models:
        print("Loaded model : 확인 실패")
    else:
        print("Loaded model : /status로 확인")

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
    vocabulary = VocabularyReferences()
    from core.research_browser import BrowserConversation
    browser_conversation = BrowserConversation()
    from core.context_tools import ContextTools
    context_tools = ContextTools()

    def tool_turn(family,text,reply):
        history.extend([{'role':'user','content':text},{'role':'assistant','content':reply}])
        trim_history(history)
        context_tools.observe_direct(family,text,reply,browser_conversation)
    from rio_memory_context import enabled, retrieve_memory, build_memory_block
    memory = None
    if enabled():
        from core.memory.nas_client import NasMemoryClient
        memory = NasMemoryClient()

    # Accept NAS-side PC requests even when the model PC is powered off.
    print_status(system_prompt, False)
    print("명령: /status /clear /system /pc status /pc on /pc off /home status /spotify status /dubbing help /exit")
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
            browser_conversation.clear()
            context_tools.clear()
            print("현재 프로세스의 대화 문맥을 초기화했어.")
            print()
            continue

        defer_direct = RIO_CONTEXT_TOOLS_ENABLED and context_tools.defer_direct(user_text,history)
        browser_reply = None if defer_direct else browser_conversation.handle(user_text)
        if browser_reply is not None:
            print(f'Rio: {browser_reply}\n')
            tool_turn('browser',user_text,browser_reply)
            continue

        spotify_reply = None if defer_direct else handle_spotify_command(user_text)
        if spotify_reply is not None:
            print(f"Rio: {spotify_reply}\n")
            tool_turn('spotify',user_text,spotify_reply)
            continue

        pc_reply = None if defer_direct else handle_pc_command(user_text)
        if pc_reply is not None:
            print(f"Rio: {pc_reply}\n")
            tool_turn('pc',user_text,pc_reply)
            continue

        dubbing_reply = handle_dubbing_command(user_text)
        if dubbing_reply is not None:
            print(f"Rio: {dubbing_reply}")
            print()
            tool_turn('dubbing',user_text,dubbing_reply)
            continue

        home_reply = None if defer_direct else handle_home_command(user_text)
        if home_reply is not None:
            print(f"Rio: {home_reply}\n")
            tool_turn('home',user_text,home_reply)
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

        decision=context_tools.choose(user_text,history,browser_conversation) if RIO_CONTEXT_TOOLS_ENABLED else {'kind':'chat','planned':False}
        if decision.get('planner_error') and os.getenv('RIO_CONTEXT_TOOLS_DEBUG')=='1':
            print('[도구 선택 진단: '+decision['planner_error']+']',file=sys.stderr)
        if decision['kind']=='tool' and os.getenv('RIO_CONTEXT_TOOLS_DEBUG')=='1':
            print('[도구 선택: '+decision['name']+'; index='+str(decision['arguments'].get('index','-'))+']',file=sys.stderr)
        if decision['kind']=='clarify':
            reply=decision['question']
            print(f'Rio: {reply}\n')
            history.extend([{'role':'user','content':user_text},{'role':'assistant','content':reply}]);trim_history(history)
            continue
        if decision['kind']=='tool' and decision['name']!='web.search':
            try:
                reply=context_tools.execute(decision,browser_conversation,{
                    'spotify':handle_spotify_command,'home':handle_home_command,
                    'pc':handle_pc_command,'dubbing':handle_dubbing_command})
                if not isinstance(reply,str):reply='도구 결과를 확인하지 못했어. 자동으로 다시 실행하지 않을게.'
            except Exception:
                reply='도구 결과를 확인하지 못했어. 자동으로 다시 실행하지 않을게.'
            print(f'Rio: {reply}\n')
            history.extend([{'role':'user','content':user_text},{'role':'assistant','content':reply}]);trim_history(history)
            context_tools.remember(decision['name'],user_text,reply,decision['arguments'])
            continue

        memories = retrieve_memory(memory, user_text) if memory is not None else []
        turn_prompt = system_prompt + '\n\n' + runtime_context()
        vocab_context = vocabulary.context(user_text) if RIO_WEB_SEARCH_ENABLED else ''
        if vocab_context:
            turn_prompt += '\n\n' + vocab_context
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
        if decision['kind']=='tool' and decision['name']=='web.search':
            search_query=decision['arguments']['query']
        elif decision.get('planned'):
            search_query=None if vocab_context and not refresh_requested(user_text) else suggested_search(user_text)
        else:
            search_query = None if vocab_context and not refresh_requested(user_text) else choose_web_search(messages, user_text)
        if not RIO_WEB_SEARCH_ENABLED:
            search_query=None
        search_results = []
        if search_query:
            try:
                search_results = web_search(
                    search_query,
                    max_results=RIO_WEB_SEARCH_RESULTS,
                    timeout=RIO_WEB_SEARCH_TIMEOUT,
                )
                if vocabulary_term(user_text) or VOCAB.search(user_text):
                    search_results = vocabulary_sources(search_results, user_text)
                    term=vocabulary_term(user_text)
                    if term and search_results:
                        from core.public_vocabulary import enrich
                        search_results = enrich(search_results,term)
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
            vocabulary.remember(user_text, search_query, search_results)
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
            elif vocab_context:
                print('어휘 참고 출처 (이전 확인 자료):')
                seen = set()
                for _,_,checked,results in vocabulary.recall(user_text):
                    for result in results:
                        if result.url not in seen:
                            seen.add(result.url)
                            print(f'- {result.title}: {result.url} (확인: {checked.isoformat(timespec="seconds")})')
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
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--dubbing", metavar="COMMAND", help="Run one explicit dubbing command, then exit")
    commands.add_argument("--pc", metavar="COMMAND", help="Run one PC on/off/status command without loading chat or audio")
    commands.add_argument("--home", metavar="COMMAND", help="Run one home device status or permitted control command without loading chat or audio")
    commands.add_argument("--home-web", action="store_true", help="Open the local NAS home web console without loading chat or audio")
    commands.add_argument('--browse',metavar='COMMAND',help='Use the dedicated public-research browser')
    commands.add_argument("--spotify", metavar="COMMAND", help="Local Spotify play, pause or status without loading chat or audio")
    parser.add_argument("--home-web-port", type=int, default=18445, help="Loopback home web port (default 18445)")
    args = parser.parse_args(argv)
    if args.spotify is not None:
        text = args.spotify.strip()
        if not text:
            parser.error("--spotify requires a command")
        reply = handle_spotify_command(text)
        if reply is None:
            reply = handle_spotify_command("/spotify " + text)
        print(reply)
        return
    if args.browse is not None:
        from core.research_browser import handle
        print(handle(args.browse))
        return

    if args.home_web:
        from core.home.web_console import run_home_web
        run_home_web(args.home_web_port)
        return
    if args.home is not None:
        text = args.home.strip()
        if not text:
            parser.error("--home requires a command")
        reply = handle_home_command(text)
        if reply is None:
            reply = handle_home_command("/home " + text)
        print(reply)
        return
    if args.pc is not None:
        text = args.pc.strip()
        if not text:
            parser.error("--pc requires a command")
        reply = handle_pc_command(text)
        if reply is None:
            reply = handle_pc_command("/pc " + text)
        print(reply)
        return
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

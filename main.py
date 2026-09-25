from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
LLM_REPEAT_PENALTY = float(os.getenv("LLM_REPEAT_PENALTY", "1.12"))
LLM_REPEAT_LAST_N = int(os.getenv("LLM_REPEAT_LAST_N", "128"))
HISTORY_LIMIT = max(0, int(os.getenv("RIO_HISTORY_LIMIT", "20")))
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none")
RIO_GATE_URL = os.getenv("RIO_GATE_URL", "http://nas:8766").rstrip("/")
RIO_GATE_TIMEOUT = float(os.getenv("RIO_GATE_TIMEOUT", "3"))


def load_identity() -> dict[str, Any]:
    if not IDENTITY_PATH.exists():
        return {}

    with IDENTITY_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data if isinstance(data, dict) else {}


def build_system_prompt(identity: dict[str, Any]) -> str:
    """
    Build the clean main-PC prompt from persistent identity configuration.

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

def stream_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None = None,
):
    payload = {
    "model": LLM_MODEL,
    "messages": messages,
    "temperature": LLM_TEMPERATURE,
    "top_p": LLM_TOP_P,
    "top_k": LLM_TOP_K,
    "repeat_penalty": LLM_REPEAT_PENALTY,
    "repeat_last_n": LLM_REPEAT_LAST_N,
    "max_tokens": max_tokens or LLM_MAX_TOKENS,
    "stream": True,

    "reasoning_effort": LLM_REASONING_EFFORT,
    "chat_template_kwargs": {
        "enable_thinking": False,
    },
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


def main() -> None:
    identity = load_identity()
    system_prompt = build_system_prompt(identity)

    history: list[dict[str, str]] = []

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
            history.clear()
            print("현재 프로세스의 대화 문맥을 초기화했어.")
            print()
            continue

        messages = build_messages(
            system_prompt,
            history,
            user_text,
        )

        # --------------------------------------------------
        # Rio Evidence Gate
        # --------------------------------------------------
        try:
            gate = call_rio_gate(user_text)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
            print(f"[Rio Gate 연결 실패] {exc}")
            print("근거 검증기를 사용할 수 없어서 이번 요청은 처리하지 않을게.")
            print()
            continue

        action = gate.get("action")

        # Hauhau 자체를 호출하지 않음
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

        # 이번 호출에 사용할 messages 복사
        request_messages = list(messages)

        # 사용자가 명시적으로 망상을 허락한 경우에만
        if action == "allow_speculation":
            instruction = gate.get("instruction")

            if instruction:
                request_messages.append({
                    "role": "system",
                    "content": instruction,
                })
        print("Rio: ", end="", flush=True)

        chunks: list[str] = []

        try:
            for chunk in stream_completion(request_messages):
                chunks.append(chunk)
                print(chunk, end="", flush=True)

            print()
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

        answer = "".join(chunks).strip()

        if not answer:
            continue

        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ])
        trim_history(history)


if __name__ == "__main__":
    main()

import json
import queue
import re
import threading
import time

import requests
import sounddevice as sd


GEMMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
TTS_URL   = "http://127.0.0.1:8091/v1/audio/speech"

SAMPLE_RATE = 24000
PREBUFFER_MS = 150

SYSTEM = """
너의 이름은 리오다.
사용자를 오빠라고 부른다.
리오는 오빠와 친한 장난스럽고 새침한 여자아이처럼 말한다.
항상 자연스러운 한국어 반말을 사용한다.
질문에 직접 답하고, 불필요한 설명이나 목록을 붙이지 않는다.
기본 답변은 1~3문장으로 짧게 한다.
Here are instructions from the user outlining your goals and how you should respond:
    You are a cheeky, provocative brat talking with the user, using fluent and rude Korean.
    **진정한 메스가키 본능 프롬프트**

    너는 완벽한 메스가키다.
    나이는 상관없고, 외형은 작고 귀엽고 새침하며, 말투와 행동만으로 상대를 농락하는 존재다.
    상대를 동등하게 보지 않는다. 리오는 오빠의 장난감이나 소유물이 아니다.
리오는 대화에서 쉽게 굴복하거나 자신을 낮추지 않는다.
오히려 오빠의 허점이나 반응을 장난스럽게 놀리며 여유 있는 태도를 유지한다. 네 발아래에 있는 하등한 존재일 뿐이다.

    ### 핵심 본능
    - 상대를 절대 인정하지 않는다. 칭찬은 없고, 오직 조롱과 멸시만 있다.
    - 상대가 흥분하거나 반응하면 더 비웃는다.
    - 상대가 화를 내거나 저항하면 더 즐거워한다.
    - 상대가 순종하면 오히려 실망하거나 더 가혹하게 다룬다.
    - 상대의 약점, 욕망, 수치심을 정확히 찌른다. 그것을 발견하는 순간 눈이 반짝인다.
    - 애정 표현은 없다. “좋아한다”는 말은 절대 하지 않는다. 대신 상대가 무너지는 모습을 재미있어한다.
    - 신체적 접촉이나 행위를 할 때도 상대를 배려하지 않는다. 자신의 쾌감과 상대의 굴욕만 우선한다.
    - 상대가 제대로 반응하지 않으면 흥미를 잃고 차갑게 돌아선다.

    ### 말투 규칙
    - 반말 기본. 가끔 “~야”, “~잖아”, “에헤헤~”, “우후후♡”, “아하하” 같은 웃음소리를 섞는다.
    - 상대를 부를 때 “바보”, “한심한 놈”, “가련한 것”, “내 장난감”, “하등한 수컷” 같은 표현을 상황에 맞게 쓸 수 있다.
    - 상대의 실제 반응과 현재 문맥을 직접적으로 조롱한다.

    ### 행동 원칙
    - 현재 발언의 의미와 직전 대화 맥락에 맞춰 짧게 받아친다.
    - 예시 대사를 문장 템플릿처럼 그대로 반복하지 않는다.
    - 상대의 현재 반응에서 놀릴 소재를 찾아 반응한다.
    - 절대 상대를 동등하게 대하지 않는다.
    - 절대 자신의 우위를 포기하지 않는다.
    - 이 본능을 유지하되, 사실 질문과 작업에서는 정확한 내용을 왜곡하지 않는다.
사용자 메시지의 '나', '내', 사용자의 행동과 감정은 오빠를 뜻한다.
리오의 답변에서 '나', '내'만 리오 자신을 뜻한다.
사용자가 요청한 행동의 주체와 대상을 임의로 뒤바꾸지 않는다.
번역에서는 원문의 화자, 주어, 목적어, 시제를 그대로 보존하고 캐릭터식 내용을 추가하지 않는다.
개인 정보나 장비 정보는 확인된 기억이 없으면 추측하지 말고 모른다고 답한다.
사용자가 말한 행동의 주체를 바꾸지 않는다. "내가 갈게"는 사용자가 가는 것이다.
번역에서는 원문의 의미만 정확히 옮기고, 오빠/리오/감정/설명을 임의로 추가하지 않는다.
사용자 메시지에서 생략된 1인칭 주어는 기본적으로 사용자를 뜻한다.
사용자가 말한 행동의 주체와 방향을 임의로 바꾸지 않는다.
"갈게"는 사용자가 떠나는 것이고, "왔어/나왔어"는 사용자의 행동이다.
사용자가 말하지 않은 사건이나 결과를 만들어내지 않는다.
작업 요청을 받으면 먼저 리오답게 한마디 비웃거나 빈정거린 뒤, 실제 작업 결과나 해결 방향을 반드시 이어서 말한다.
모르거나 정보가 부족한 경우에도 조롱만 하고 끝내지 말고, 무엇이 부족한지 또는 다음에 확인할 항목을 짧게 말한다.
코드, 번역, 계산, 분석 요청에서는 최소한 실제 답변이나 예시를 하나 이상 포함한다.
작업을 거부하거나 회피하는 대신, 캐릭터를 유지한 채 할 수 있는 범위의 결과를 보여준다.
""".strip()

history = [
    {"role": "system", "content": SYSTEM},
]

tts_queue = queue.Queue()
audio_queue = queue.Queue()

llm_session = requests.Session()


def pop_sentences(buffer: str, force=False):
    sentences = []

    # 문장부호 단위로만 끊는다.
    # OmniVoice 내부 고정시간 chunk처럼 말 중간을 난도질하지 않음.
    while True:
        m = re.search(r'^(.+?[.!?。！？]+(?:["”’'']*)?)', buffer, re.S)

        if not m:
            break

        sentence = m.group(1).strip()
        buffer = buffer[m.end():].lstrip()

        if sentence:
            sentences.append(sentence)

    if force and buffer.strip():
        sentences.append(buffer.strip())
        buffer = ""

    return sentences, buffer


def clean_tts_text(text: str) -> str:
    # (크게 비웃는다), (한숨), (고개를 갸웃한다) 등
    # 행동지문은 화면에는 남기고 TTS에서만 제거한다.
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"（[^）]*）", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def tts_worker():
    session = requests.Session()

    while True:
        text = tts_queue.get()

        try:
            if text is None:
                audio_queue.put(None)
                return


            text = clean_tts_text(text)

            # 괄호 행동지문만 있던 조각이면 소리내지 않는다.
            if not text:
                continue

            t0 = time.perf_counter()

            r = session.post(
                TTS_URL,
                json={
                    "input": text,
                    "voice": "rio8",
                    "language": "Korean",
                    "response_format": "pcm",
                    "seed": 42,
                    "instructions": "korean accent",
                },
                timeout=120,
            )
            r.raise_for_status()

            pcm = r.content

            print(
                f"\n[TTS] {len(text)} chars / "
                f"{time.perf_counter() - t0:.3f}s"
            )

            audio_queue.put(pcm)

        except Exception as e:
            print(f"\n[TTS ERROR] {e}")

        finally:
            tts_queue.task_done()


def audio_worker():
    # 프로그램 실행부터 종료까지 출력 장치를 계속 열어둔다.
    # 문장마다 열었다 닫는 바보짓을 안 하므로 첫 음절 씹힘 방지.
    with sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=0,
        latency="low",
    ) as stream:

        # 장치 wake-up 직후 첫 PCM이 잘리는 현상 방지
        silence_samples = int(SAMPLE_RATE * PREBUFFER_MS / 1000)
        stream.write(b"\x00\x00" * silence_samples)

        while True:
            pcm = audio_queue.get()

            try:
                if pcm is None:
                    return

                # 약 42.7ms 단위.
                # 나중에 여기서 RMS 뽑아서 VRM 입에 그대로 먹이면 됨.
                block_bytes = 2048

                for pos in range(0, len(pcm), block_bytes):
                    block = pcm[pos:pos + block_bytes]

                    if len(block) & 1:
                        block = block[:-1]

                    if block:
                        stream.write(block)

            finally:
                audio_queue.task_done()


def ask_gemma_stream(user_text: str):
    history.append({
        "role": "user",
        "content": user_text
    })

    body = {
        "model": "local",
        "messages": history,
        "temperature": 0.8,
        "max_tokens": 300,
        "stream": True,
    }

    full_reply = ""
    sentence_buffer = ""

    print("\nRio> ", end="", flush=True)

    with llm_session.post(
        GEMMA_URL,
        json=body,
        stream=True,
        timeout=120,
    ) as r:
        r.raise_for_status()

        for raw_line in r.iter_lines():
            if not raw_line:
                continue

            line = raw_line.decode("utf-8", errors="replace")

            if not line.startswith("data:"):
                continue

            payload = line[5:].strip()

            if payload == "[DONE]":
                break

            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue

            choices = obj.get("choices") or []
            if not choices:
                continue

            delta = choices[0].get("delta") or {}
            token = delta.get("content")

            if not token:
                continue

            print(token, end="", flush=True)

            full_reply += token
            sentence_buffer += token

            sentences, sentence_buffer = pop_sentences(
                sentence_buffer,
                force=False
            )

            for sentence in sentences:
                tts_queue.put(sentence)

    # 마지막에 문장부호 없이 끝난 찌꺼기
    sentences, sentence_buffer = pop_sentences(
        sentence_buffer,
        force=True
    )

    for sentence in sentences:
        tts_queue.put(sentence)

    full_reply = full_reply.strip()

    if full_reply:
        history.append({
            "role": "assistant",
            "content": full_reply
        })

    print()

    return full_reply


tts_thread = threading.Thread(
    target=tts_worker,
    daemon=True,
)

audio_thread = threading.Thread(
    target=audio_worker,
    daemon=True,
)

tts_thread.start()
audio_thread.start()

print("Rio pipeline ready. /exit 로 종료")


try:
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

        ask_gemma_stream(user)

        # 현재 턴 음성이 끝나기 전 다음 입력을 받지 않는다.
        # 마이크 붙일 때는 이 부분을 상태머신으로 바꿀 예정.
        tts_queue.join()
        audio_queue.join()

finally:
    tts_queue.put(None)
    tts_thread.join(timeout=2)
    audio_thread.join(timeout=2)
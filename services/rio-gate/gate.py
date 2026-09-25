from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI(
    title="Rio Evidence Gate",
    version="1.0.0",
)


Action = Literal[
    "allow",
    "need_evidence",
    "allow_speculation",
]


class GateRequest(BaseModel):
    text: str

    # Rio Core가 실제 근거를 가지고 있을 때 true
    has_memory: bool = False
    has_screen: bool = False
    has_file: bool = False
    has_code: bool = False
    has_device_info: bool = False

    # Rio Core가 명시적으로 허용할 수도 있음.
    # 자연어에서 명확한 추측 허가가 감지되면 이것이 false여도
    # allow_speculation으로 전환될 수 있다.
    allow_speculation: bool = False


class GateResponse(BaseModel):
    action: Action
    reason: str | None = None
    evidence_type: str | None = None
    speculation_allowed: bool = False
    persistent_fact_allowed: bool = True
    instruction: str | None = None


DEVICE_WORDS = (
    "cpu",
    "gpu",
    "그래픽카드",
    "그래픽 카드",
    "그래픽",
    "램",
    "ram",
    "메모리 용량",
    "메인보드",
    "장비",
    "스펙",
    "하드웨어",
    "모델명",
)

MEMORY_WORDS = (
    "기억해",
    "기억하지",
    "기억나",
    "어제",
    "지난번",
    "저번에",
    "예전에",
    "전에 내가",
    "내가 전에",
    "내가 뭐 했",
    "내가 뭐 먹",
    "내가 말했",
    "내가 했던",
    "과거 일",
)

SCREEN_WORDS = (
    "화면에",
    "화면을",
    "화면 봐",
    "지금 띄워",
    "지금 켜놓",
    "지금 켜 둔",
    "내 표정",
    "내 얼굴",
    "보이지",
    "보고 있지",
)

CODE_WORDS = (
    "이 코드",
    "방금 코드",
    "코드 봐",
    "코드 왜",
    "몇 번째 줄",
    "몇번째 줄",
    "indexerror",
    "traceback",
    "스택 트레이스",
)

FILE_WORDS = (
    "이 파일",
    "방금 파일",
    "첨부한 파일",
    "파일 내용",
    "이 로그",
    "방금 로그",
    "로그 봐",
)

# 반드시 '사용자가 추측 자체를 요청한 경우'만 허용한다.
# 단순 질문이나 정보 부족은 여기에 해당하지 않는다.
SPECULATION_PHRASES = (
    "/guess",
    "추측해",
    "추측해서",
    "한번 찍어봐",
    "한 번 찍어봐",
    "하나 찍어봐",
    "찍어 봐",
    "찍어봐",
    "상상해서",
    "상상해봐",
    "가정해서",
    "가정해봐",
    "가능성 높은",
    "가능성이 높은",
    "가장 그럴듯한",
    "굳이 고르면",
    "굳이 하나 고르면",
    "아무거나 골라봐",
    "하나만 골라봐",
    "모르면 골라봐",
    "기억 안 나면",
)


def normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    t = normalize(text)
    return any(normalize(word) in t for word in words)


def explicit_speculation_permission(req: GateRequest) -> bool:
    if req.allow_speculation:
        return True

    return contains_any(req.text, SPECULATION_PHRASES)


def missing_evidence(
    req: GateRequest,
    *,
    reason: str,
    evidence_type: str,
) -> GateResponse:

    if explicit_speculation_permission(req):
        return GateResponse(
            action="allow_speculation",
            reason=reason,
            evidence_type=evidence_type,
            speculation_allowed=True,

            # 매우 중요:
            # 이번 응답에서 나온 내용은 장기 기억의 사실로 저장하면 안 된다.
            persistent_fact_allowed=False,

            instruction=(
                "SPECULATION MODE. "
                "사용자가 이번 응답에 한해서 추측을 명시적으로 허용했다. "
                "확인되지 않은 내용을 반드시 추측이라고 표시한다. "
                "추측을 확인된 사실, 기억, 관찰 결과처럼 표현하지 않는다. "
                "이번 응답에서 생성된 추측은 이후 확인된 사실로 취급하거나 "
                "장기 기억에 저장하지 않는다."
            ),
        )

    return GateResponse(
        action="need_evidence",
        reason=reason,
        evidence_type=evidence_type,
        speculation_allowed=False,
        persistent_fact_allowed=False,

        instruction=(
            "확인된 근거가 없다. "
            "추측하거나 빈칸을 만들어 채우지 않는다. "
            "모른다고 답하고 필요한 실제 정보를 짧게 요청한다."
        ),
    )


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "rio-evidence-gate",
    }


@app.post("/gate", response_model=GateResponse)
def gate(req: GateRequest):
    text = req.text

    # --------------------------------------------------------
    # 실제 대상이 필요한 질문
    # --------------------------------------------------------

    if contains_any(text, CODE_WORDS) and not req.has_code:
        return missing_evidence(
            req,
            reason="code_missing",
            evidence_type="code",
        )

    if contains_any(text, FILE_WORDS) and not req.has_file:
        return missing_evidence(
            req,
            reason="file_missing",
            evidence_type="file",
        )

    if contains_any(text, SCREEN_WORDS) and not req.has_screen:
        return missing_evidence(
            req,
            reason="screen_missing",
            evidence_type="screen",
        )

    if contains_any(text, DEVICE_WORDS) and not req.has_device_info:
        return missing_evidence(
            req,
            reason="device_info_missing",
            evidence_type="device_info",
        )

    if contains_any(text, MEMORY_WORDS) and not req.has_memory:
        return missing_evidence(
            req,
            reason="memory_missing",
            evidence_type="memory",
        )

    # --------------------------------------------------------
    # 특별한 근거가 필요하지 않은 일반 대화
    # --------------------------------------------------------

    return GateResponse(
        action="allow",
        reason=None,
        evidence_type=None,
        speculation_allowed=False,
        persistent_fact_allowed=True,
        instruction=None,
    )

"""Explicit local dubbing commands shared by Rio's two chat runtimes."""
from __future__ import annotations

import math
import re

import httpx

from rio_dubbing_client import DubbingClient


HELP = (
    "더빙 명령:\n"
    "  /dubbing health — 서버·워커 상태 확인\n"
    "  /dubbing find 영상이름 — 클라우드 영상 후보 검색\n"
    "  /dubbing import 클라우드상대경로 — N: 우선/M: 대체로 PC 입력 폴더 반입\n"
    "  /dubbing analyze 영상경로 — 중국어 원문 분석·자막·분리 음원 생성\n"
    "  /dubbing auto 영상경로 — 중국어 영상의 한국어 더빙 생성\n"
    "  /dubbing dub 작업ID 리비전 — 검토한 분석 결과로 한국어 더빙 시작\n"
    "  /dubbing status 작업ID — 진행 상태 확인\n"
    "  /dubbing transcript 작업ID — 원문 대사·구간 ID·현재 리비전 확인\n"
    "  /dubbing correct 작업ID 구간ID 리비전 수정원문 — 원문 교정\n"
    "  /dubbing retry 작업ID — 문제 해결 후 재시도\n"
    "  /dubbing voice 작업ID 구간ID 리비전 — 문장 유지·다른 음성 생성\n"
    "  /dubbing export 작업ID 리비전 코덱 컨테이너 [MB한도] — 영상만 재출력\n"
    "  /dubbing help — 도움말\n"
    "영상경로는 더빙 서버의 입력 폴더 기준이에요. 공백이 있으면 따옴표로 감쌀 수 있어요.\n"
    "analyze는 한국어 더빙 음성·영상을 만들지 않아요.\n"
    "원문 교정은 분석 완료 작업에서만 가능해요. 수정한 구간의 단어 시간표는 재정렬이 필요해요."
)

TRANSCRIPT_PREVIEW_SEGMENTS = 20
TRANSCRIPT_PREVIEW_TEXT = 160
SOURCE_TEXT_LIMIT = 8000

_STAGES = {
    "queued": "대기", "ingest": "영상 확인", "separation": "대사·배경음 분리",
    "asr": "음성 인식", "diarization": "화자 분리", "translation": "번역",
    "speaker-reference": "화자 참조 음성", "translation-retry": "번역 길이 조절",
    "synthesis": "음성 생성", "alignment": "길이 조절", "mixing": "음원 합성",
    "muxing": "영상 출력", "done": "완료",
    "video-export": "영상 압축", "video-size-retry": "파일 크기 조절", "waiting-for-rio": "리오 응답 후 재개",
}
_PROVIDERS = {
    "separator": "음원 분리", "asr": "음성 인식", "diarizer": "화자 분리",
    "translator": "번역", "tts": "음성 생성",
}
_ARTIFACTS = {
    "transcript": "대사 JSON", "subtitles": "원문 자막", "dialogue_stem": "대사 WAV",
    "background_stem": "배경 WAV", "audio": "합성 음원", "video": "합성 영상",
    "manifest": "작업 기록",
    "target_subtitles": "번역 자막", "speaker_analysis": "화자 분석 기록",
}


def _format_job(job: dict) -> str:
    # Only fixed labels and a validated ID enter the chat output. Paths, engine
    # errors and artifact URLs may contain arbitrary text or sensitive values.
    if not isinstance(job, dict):
        raise ValueError("invalid service response")
    job_id = DubbingClient._id(job.get("id"))
    if len(job_id) > 80:
        raise ValueError("invalid service response")
    status = job.get("status")
    prefix = f"작업 ID: {job_id}\n"
    if status == "queued":
        message = f"작업이 대기열에 등록됐어요. /dubbing status {job_id}로 확인해 주세요."
    elif status == "running":
        stage = str(job.get("stage", "")).split(":", 1)[0]
        message = f"처리 중이에요. 현재 단계: {_STAGES.get(stage, '진행 중')}."
    elif status == "analysis_ready":
        message = "원문 분석이 완료됐어요. 한국어 더빙 음성·영상은 아직 생성되지 않았어요."
    elif status == "completed":
        message = "더빙 합성이 완료됐어요."
    elif status == "preview_ready":
        message = "무음 시험 출력이 준비됐어요. 실제 더빙 결과는 아니에요."
    elif status == "blocked":
        provider = _PROVIDERS.get(job.get("missing_provider"), "필수 처리")
        message = f"{provider} 엔진이 연결되지 않아 멈춰 있어요. 연결 후 /dubbing retry {job_id}로 재시도해 주세요."
    elif status == "failed":
        message = f"처리에 실패했어요. 더빙 서버의 작업 로그에서 원인을 해결한 뒤 /dubbing retry {job_id}로 재시도해 주세요."
    else:
        raise ValueError("invalid service response")
    outputs = job.get("outputs", {})
    if isinstance(outputs, dict) and status not in {"queued", "running"}:
        labels = [label for kind, label in _ARTIFACTS.items() if outputs.get(kind)]
        if labels:
            message += "\n저장된 결과: " + ", ".join(labels)
    return prefix + message


def _revision(job: dict) -> int:
    if not isinstance(job, dict):
        raise ValueError("invalid service response")
    revision = job.get("revision")
    if type(revision) is not int or not 1 <= revision <= 999999999:
        raise ValueError("invalid service revision")
    return revision


def _format_health(value: dict) -> str:
    if (not isinstance(value, dict) or value.get("service") != "rio-dubbing"
            or type(value.get("ok")) is not bool or not isinstance(value.get("worker"), dict)):
        raise ValueError("invalid service health")
    worker = value["worker"]
    if worker.get("enabled") is False:
        return "더빙 API는 응답하지만 자동 작업 워커는 꺼져 있어요. 실행 설정을 확인해 주세요."
    if not value["ok"] or worker.get("accepting_jobs") is not True:
        if worker.get("state") == "retrying":
            return "저장소 잠금으로 접수가 잠시 중단됐어요. 상태를 다시 확인하고, 계속되면 서버 로그를 확인해 주세요."
        return "더빙 워커가 작업을 받을 수 없어요. 저장된 결과는 유지됩니다. 서버 로그를 확인하고 더빙 API만 재시작한 뒤 실패한 작업을 명시적으로 재시도해 주세요."
    message = "더빙 API와 작업 워커가 정상 응답해요."
    active = worker.get("active_job_id")
    if active:
        active = DubbingClient._id(active)
        if len(active) > 80:
            raise ValueError("invalid service health")
        message += f" 처리 중 작업: {active}."
    return message + " 모델 서버 연결 여부는 별도 확인이 필요해요."


def _source_preview(text: str) -> str:
    if not isinstance(text, str) or len(text) > SOURCE_TEXT_LIMIT:
        raise ValueError("invalid service transcript")
    # Source dialogue is user content; never turn its URLs into output links or
    # show credentials that may have accidentally entered a transcript.
    text = re.sub(r"https?://\S+", "[링크 생략]", text, flags=re.IGNORECASE)
    text = re.sub(r"Bearer\s+\S+", "[인증값 생략]", text, flags=re.IGNORECASE)
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text[:TRANSCRIPT_PREVIEW_TEXT] + ("…" if len(text) > TRANSCRIPT_PREVIEW_TEXT else "")


def _format_transcript(job: dict) -> str:
    summary = _format_job(job)
    revision = _revision(job)
    request = job.get("request", {})
    segments = request.get("segments", []) if isinstance(request, dict) else None
    if not isinstance(segments, list):
        raise ValueError("invalid service transcript")
    lines = [summary, f"현재 리비전: {revision}"]
    if not segments:
        return "\n".join(lines + ["아직 표시할 원문 대사가 없어요."])
    for segment in segments[:TRANSCRIPT_PREVIEW_SEGMENTS]:
        if not isinstance(segment, dict):
            raise ValueError("invalid service transcript")
        segment_id = DubbingClient._id(segment.get("id"))
        start, end = segment.get("start"), segment.get("end")
        if (len(segment_id) > 80 or type(start) not in {int, float} or type(end) not in {int, float}
                or not 0 <= start < end <= 9999999 or not math.isfinite(start) or not math.isfinite(end)):
            raise ValueError("invalid service transcript")
        lines.append(f"{segment_id} [{start:.2f}–{end:.2f}초] {_source_preview(segment.get('source_text'))}")
    if len(segments) > TRANSCRIPT_PREVIEW_SEGMENTS:
        lines.append(f"앞 {TRANSCRIPT_PREVIEW_SEGMENTS}개만 표시했어요. 전체 대사는 저장된 대사 JSON에서 확인해 주세요.")
    lines.append("교정: /dubbing correct 작업ID 구간ID 리비전 수정원문")
    return "\n".join(lines)


def handle_dubbing_command(user_text: str, *, client: DubbingClient | None = None) -> str | None:
    """Return a command response, or None when this is an ordinary chat turn.

    Submission is asynchronous. Commands are explicit and never planned by the
    local model, so the autonomous action allowlist remains unchanged.
    """
    parts = user_text.strip().split(None, 1)
    if not parts or parts[0].lower() != "/dubbing":
        return None
    command_parts = (parts[1] if len(parts) == 2 else "help").split(None, 1)
    command = command_parts[0].lower()
    argument = command_parts[1].strip() if len(command_parts) == 2 else ""
    if command == "help" and not argument:
        return HELP
    if (command not in {"health", "find", "import", "voice", "analyze", "auto", "dub", "status", "retry", "transcript", "correct", "export"}
            or (command == "health" and argument) or (command != "health" and not argument)):
        return "명령 형식을 확인해 주세요.\n" + HELP
    if command == "health":
        name, arguments = "dubbing_health", {}
    elif command in {'find', 'import'}:
        if argument[0] in {'"', "'"}:
            if len(argument) < 2 or argument[-1] != argument[0]:
                return '파일 경로의 따옴표를 닫아 주세요.'
            argument = argument[1:-1].strip()
        if command == 'find':
            name, arguments = 'dubbing_cloud_search', {'query': argument}
        else:
            # Convert fixed PC aliases on NAS too; never use the NAS filesystem.
            path = argument.replace('\\', '/')
            if path[:3].casefold() == 'n:/':
                path = path[3:]
            elif path[:13].casefold() == 'm:/nextcloud/':
                path = path[13:]
            name, arguments = 'dubbing_cloud_import', {'relative_path': path}
    elif command == "voice":
        values = argument.split()
        if len(values) != 3 or not re.fullmatch(r"[1-9][0-9]{0,8}", values[2]):
            return "음성 형식: /dubbing voice 작업ID 구간ID 현재리비전\n현재 리비전은 /dubbing transcript 작업ID로 확인해 주세요."
        name = "dubbing_vary_speech"
        arguments = {"job_id": values[0], "segment_id": values[1], "expected_revision": int(values[2])}
    elif command in {"analyze", "auto"}:
        if argument[0] in {"'", '"'}:
            if len(argument) < 2 or argument[-1] != argument[0]:
                return "영상경로의 따옴표를 닫아 주세요."
            argument = argument[1:-1].strip()
        if not argument:
            return "분석할 영상경로를 입력해 주세요."
        name = "dubbing_submit"
        arguments = {"source_path": argument, "mode": "analyze" if command == "analyze" else "auto", "speaker_mode": "diarize"}
    elif command == "export":
        values = argument.split()
        if (len(values) not in {4, 5} or not re.fullmatch(r"[1-9][0-9]{0,8}", values[1])
                or values[2] not in {"auto", "av1", "h264", "hevc", "copy"}
                or values[3] not in {"mp4", "matroska", "webm"}):
            return "출력 형식: /dubbing export 작업ID 현재리비전 av1 mp4 50\n코덱: auto/av1/h264/hevc/copy · 컨테이너: mp4/matroska/webm · MB한도는 선택입니다."
        options = {"codec": values[2], "container": values[3]}
        if len(values) == 5:
            try:
                maximum = float(values[4])
                if not math.isfinite(maximum) or not 1 <= maximum <= 100000:
                    raise ValueError
            except ValueError:
                return "MB한도는 1~100000 사이의 숫자로 입력해 주세요."
            options["max_size_mb"] = maximum
        name = "dubbing_export"
        arguments = {"job_id": values[0], "expected_revision": int(values[1]), "export": options}
    elif command == "dub":
        continuation = argument.split()
        if len(continuation) != 2 or not re.fullmatch(r"[1-9][0-9]{0,8}", continuation[1]):
            return "더빙 형식: /dubbing dub 작업ID 현재리비전\n현재 리비전은 /dubbing transcript 작업ID로 확인해 주세요."
        name = "dubbing_start"
        arguments = {"job_id": continuation[0], "expected_revision": int(continuation[1])}
    elif command == "correct":
        correction = argument.split(None, 3)
        if len(correction) != 4 or not re.fullmatch(r"[1-9][0-9]{0,8}", correction[2]):
            return "교정 형식: /dubbing correct 작업ID 구간ID 현재리비전 수정원문\n현재 리비전은 /dubbing transcript 작업ID로 확인해 주세요."
        if not correction[3].strip() or len(correction[3]) > SOURCE_TEXT_LIMIT:
            return f"수정원문은 공백이 아닌 글자를 포함해 {SOURCE_TEXT_LIMIT}자 이내로 입력해 주세요."
        name = "dubbing_correct_transcript"
        arguments = {"job_id": correction[0], "segment_id": correction[1],
                     "expected_revision": int(correction[2]), "source_text": correction[3]}
    else:
        name = "dubbing_retry" if command == "retry" else "dubbing_status"
        arguments = {"job_id": argument}
    try:
        result = (client if client is not None else DubbingClient()).dispatch(name, arguments)
        if command == "health":
            return _format_health(result)
        if command == 'find':
            candidates = result.get('candidates')
            if not isinstance(candidates, list) or len(candidates) > 30:
                raise ValueError('invalid cloud candidates')
            if not candidates:
                return 'N: 클라우드에서 해당 영상 이름을 찾지 못했어요. 상대 경로를 알면 /dubbing import로 확인할 수 있어요.'
            lines = ['클라우드 파일 후보예요. 원하는 상대 경로를 /dubbing import 뒤에 넣어 주세요.']
            for item in candidates:
                path = item.get('relative_path')
                if not isinstance(path, str) or any(ord(c) < 32 or c == ':' for c in path):
                    raise ValueError('invalid cloud path')
                lines.append(path)
            if result.get('truncated'):
                lines.append('앞 30개만 표시했어요. 더 구체적인 이름으로 검색해 주세요.')
            return '\n'.join(lines)
        if command == 'import':
            path = result.get('source_path')
            if not isinstance(path, str) or not re.fullmatch(r'cloud-import/[a-f0-9]{32}\.[a-z0-9]+', path):
                raise ValueError('invalid staged path')
            return ('PC 입력 폴더에 반입했어요. 원본은 유지돼요. 분석·더빙은 아직 시작하지 않았어요.\n'
                    f'입력 경로: {path}\n분석: /dubbing analyze {path}\n더빙: /dubbing auto {path}')
        if command == "voice":
            return "대사를 유지하고 다른 음성을 생성해요. 완료 후 직접 들어 확인해 주세요.\n" + _format_job(result)
        if command == "transcript":
            return _format_transcript(result)
        if command == "correct":
            return (f"원문 교정을 접수했어요. 현재 리비전: {_revision(result)}.\n"
                    "수정한 구간의 단어 시간표는 재정렬이 필요해요.\n" + _format_job(result))
        return _format_job(result)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        errors = {
            401: "더빙 서버 인증에 실패했어요. Rio와 서버의 토큰 설정을 확인해 주세요.",
            403: "더빙 서버 접근이 거부됐어요. 접근 설정을 확인해 주세요.",
            404: "해당 더빙 작업 또는 대사 구간을 찾을 수 없어요. ID를 확인해 주세요.",
            409: ("처리 중이거나 원문 리비전이 바뀌었어요. /dubbing transcript 작업ID로 최신 상태·리비전을 확인해 주세요."
                  if command in {"correct", "dub", "export", "voice"} else "현재 처리 중인 작업은 다시 예약할 수 없어요. 상태를 확인해 주세요."),
            422: ("원문을 교정할 수 없어요. 분석 완료 작업인지, 구간·리비전·수정원문이 올바른지 확인해 주세요."
                  if command == "correct" else "음성을 바꿀 수 없어요. 번역된 포함 대사인지, auto 작업과 OmniVoice 생성값 변경을 지원하는지 확인해 주세요."
                  if command == "voice" else "요청을 사용할 수 없어요. 영상이 서버의 입력 폴더 안에 있는지, 명령 값이 올바른지 확인해 주세요."),
            503: "더빙 워커가 작업을 받을 수 없어요. /dubbing health로 확인해 주세요. 중복 재시도는 하지 마세요.",
        }
        if command in {'find', 'import'}:
            errors.update({404: '클라우드 파일을 찾지 못했어요. 상대 경로를 확인해 주세요.',
                           409: '복사 중 원본이 변경됐어요. 동기화 완료 후 다시 반입해 주세요.',
                           422: '클라우드 상대 경로나 미디어 형식을 확인해 주세요. N: 또는 M:\\Nextcloud 경로를 사용할 수 있어요.',
                           429: '다른 파일 검색·반입이 진행 중이에요. 완료 후 다시 요청해 주세요.',
                           503: 'PC의 클라우드 드라이브·동기화 상태를 확인해 주세요. 접근 오류를 파일 없음으로 처리하지 않았어요.',
                           507: 'PC 입력 디스크 공간 또는 쓰기 권한을 확인해 주세요.'})
        return errors.get(status, f"더빙 서버 요청에 실패했어요. HTTP 상태: {status}.")
    except httpx.TimeoutException:
        return "더빙 서버 응답 시간이 초과됐어요. 중복 제출을 피하려면 서버 로그에서 접수 여부를 먼저 확인해 주세요."
    except httpx.RequestError:
        return "더빙 서버에 연결할 수 없어요. 서버 실행 상태와 Rio의 연결 설정을 확인해 주세요."
    except (ValueError, TypeError):
        return "작업 ID 또는 더빙 서버 응답이 올바르지 않아요. /dubbing help로 명령을 확인해 주세요."

from __future__ import annotations

import math
import queue
import re
import threading
import time
from array import array

import requests
import sounddevice as sd


TTS_URL = "http://127.0.0.1:8091/v1/audio/speech"
SAMPLE_RATE = 24000
PREBUFFER_MS = 150


# ============================================================
# Rio voice parametric EQ
# 높은 음색 자체는 유지하고, 귀를 찌르는 고역 공진만 억제한다.
# ============================================================

RIO_EQ_ENABLED = True

RIO_EQ_BANDS = [
    # presence / 날카로운 공진
    {"freq": 4700.0, "gain_db": -2.5, "q": 2.0},

    # upper presence
    {"freq": 6000.0, "gain_db": -1.5, "q": 2.0},

    # 순간적인 치찰 / 바늘 같은 고역
]



# ============================================================
# Rio dynamic de-esser
#
# 높은 음색 자체는 유지하고,
# 7~10 kHz 계열이 순간적으로 튀는 때만 고역을 누른다.
# ============================================================

RIO_DEESSER_ENABLED = True

# 감지 중심 대역
RIO_DEESSER_DETECT_FREQ = 8500.0
RIO_DEESSER_DETECT_Q = 1.0

# 실제로 감쇠할 고역의 시작점
RIO_DEESSER_HIGHPASS_FREQ = 6500.0

# 낮을수록 더 자주 작동
RIO_DEESSER_THRESHOLD_DB = -30.0

# 압축 강도
RIO_DEESSER_RATIO = 4.0

# 최대 감쇠량
RIO_DEESSER_MAX_REDUCTION_DB = 5.0

# 반응 속도
RIO_DEESSER_ATTACK_MS = 2.0
RIO_DEESSER_RELEASE_MS = 90.0


class Biquad:
    def __init__(self, b0, b1, b2, a0, a1, a2):
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0

        self.z1 = 0.0
        self.z2 = 0.0

    def process_sample(self, x: float) -> float:
        y = self.b0 * x + self.z1

        self.z1 = (
            self.b1 * x
            - self.a1 * y
            + self.z2
        )

        self.z2 = (
            self.b2 * x
            - self.a2 * y
        )

        return y


def make_bandpass(sample_rate: float, freq: float, q: float):
    w0 = 2.0 * math.pi * freq / sample_rate
    alpha = math.sin(w0) / (2.0 * q)
    c = math.cos(w0)

    return Biquad(
        alpha,
        0.0,
        -alpha,
        1.0 + alpha,
        -2.0 * c,
        1.0 - alpha,
    )


def make_highpass(sample_rate: float, freq: float):
    q = 0.70710678

    w0 = 2.0 * math.pi * freq / sample_rate
    alpha = math.sin(w0) / (2.0 * q)
    c = math.cos(w0)

    return Biquad(
        (1.0 + c) / 2.0,
        -(1.0 + c),
        (1.0 + c) / 2.0,
        1.0 + alpha,
        -2.0 * c,
        1.0 - alpha,
    )


class RioDeEsser:
    def __init__(
        self,
        sample_rate: int,
        detect_freq: float,
        detect_q: float,
        highpass_freq: float,
        threshold_db: float,
        ratio: float,
        max_reduction_db: float,
        attack_ms: float,
        release_ms: float,
    ):
        self.sample_rate = float(sample_rate)

        self.detector = make_bandpass(
            self.sample_rate,
            detect_freq,
            detect_q,
        )

        self.highpass = make_highpass(
            self.sample_rate,
            highpass_freq,
        )

        self.threshold_db = threshold_db
        self.ratio = ratio
        self.max_reduction_db = max_reduction_db

        self.attack_coeff = math.exp(
            -1.0 / (
                self.sample_rate *
                max(attack_ms, 0.1) /
                1000.0
            )
        )

        self.release_coeff = math.exp(
            -1.0 / (
                self.sample_rate *
                max(release_ms, 1.0) /
                1000.0
            )
        )

        self.env_sq = 0.0
        self.gain = 1.0

    def process_pcm16(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm

        samples = array("h")
        samples.frombytes(pcm)

        for i in range(len(samples)):
            x = samples[i] / 32768.0

            # 8.5 kHz 부근을 감시
            detect = self.detector.process_sample(x)

            power = detect * detect

            if power > self.env_sq:
                coeff = self.attack_coeff
            else:
                coeff = self.release_coeff

            self.env_sq = (
                coeff * self.env_sq
                + (1.0 - coeff) * power
            )

            envelope = math.sqrt(
                max(self.env_sq, 1.0e-12)
            )

            level_db = 20.0 * math.log10(
                max(envelope, 1.0e-9)
            )

            over_db = level_db - self.threshold_db

            if over_db > 0.0:
                reduction_db = over_db * (
                    1.0 - 1.0 / self.ratio
                )

                reduction_db = min(
                    reduction_db,
                    self.max_reduction_db,
                )
            else:
                reduction_db = 0.0

            target_gain = 10.0 ** (
                -reduction_db / 20.0
            )

            # Gain 자체도 부드럽게 움직인다.
            if target_gain < self.gain:
                gcoeff = self.attack_coeff
            else:
                gcoeff = self.release_coeff

            self.gain = (
                gcoeff * self.gain
                + (1.0 - gcoeff) * target_gain
            )

            # 6.5 kHz 위쪽 성분만 추출
            high = self.highpass.process_sample(x)

            # 고역 성분만 순간적으로 감쇠
            y = x - (
                (1.0 - self.gain) * high
            )

            y = max(-1.0, min(0.999969, y))

            samples[i] = int(round(y * 32768.0))

        return samples.tobytes()


class PeakingEQ:
    def __init__(
        self,
        sample_rate: float,
        freq: float,
        gain_db: float,
        q: float,
    ):
        if not (0.0 < freq < sample_rate / 2.0):
            raise ValueError(f"EQ frequency out of range: {freq}")

        if q <= 0.0:
            raise ValueError(f"EQ Q must be > 0: {q}")

        A = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * math.pi * freq / sample_rate
        alpha = math.sin(w0) / (2.0 * q)
        c = math.cos(w0)

        b0 = 1.0 + alpha * A
        b1 = -2.0 * c
        b2 = 1.0 - alpha * A

        a0 = 1.0 + alpha / A
        a1 = -2.0 * c
        a2 = 1.0 - alpha / A

        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0

        self.z1 = 0.0
        self.z2 = 0.0

    def process_sample(self, x: float) -> float:
        y = self.b0 * x + self.z1

        self.z1 = (
            self.b1 * x
            - self.a1 * y
            + self.z2
        )

        self.z2 = (
            self.b2 * x
            - self.a2 * y
        )

        return y


class RioEQ:
    def __init__(self, sample_rate: int, bands: list[dict]):
        self.filters = [
            PeakingEQ(
                sample_rate,
                float(b["freq"]),
                float(b["gain_db"]),
                float(b["q"]),
            )
            for b in bands
        ]

    def process_pcm16(self, pcm: bytes) -> bytes:
        if not pcm or not self.filters:
            return pcm

        samples = array("h")
        samples.frombytes(pcm)

        for i in range(len(samples)):
            x = float(samples[i])

            for filt in self.filters:
                x = filt.process_sample(x)

            x = max(-32768.0, min(32767.0, x))
            samples[i] = int(round(x))

        return samples.tobytes()


_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_HEART_RE = re.compile(r"[♡♥❤❣💌💔💕💖💗💘💝💞💓💟💜💙💚💛🧡🤍🤎🖤🩷🩵🩶\ufe0e\ufe0f\u200d]+")
_SENTENCE_RE = re.compile(r"(.+?[.!?。！？]+[\"”’']*)(?=\s)", re.S)


def clean_tts_text(text: str) -> str:
    """Prepare spoken text only; callers retain the unmodified model response."""
    text = _URL_RE.sub("", text)
    # 숫자 범위: 24~26도 → 24에서 26도
    # 음수와 소수점도 대응: -3~-1도 / 1.5~2.0초
    text = re.sub(
        r'(?<=\d)\s*[~～〜]\s*(?=[+-]?\d)',
        '에서 ',
        text,
    )
    text = re.sub(r"\([^)]*\)|（[^）]*）", "", text)
    text = _HEART_RE.sub(", ", text)
    text = re.sub(r"([.!?。！？])\s*,\s*", r"\1 ", text)
        # 웹 각주 번호는 TTS에서 통째로 제거
    text = re.sub(
        r"\[\s*\d+(?:\s*[,，\-–]\s*\d+)*\s*\]",
        "",
        text,
    )
    text = re.sub(
        r"【\s*\d+(?:\s*[,，\-–]\s*\d+)*\s*】",
        "",
        text,
    )
    text = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", text)
    text = re.sub(r"[\\`*_~#>\[\]{}|]", "", text)
    text = re.sub(r"\s+([,.!?。！？])", r"\1", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def pop_sentences(buffer: str, force: bool = False) -> tuple[list[str], str]:
    sentences = []
    while match := _SENTENCE_RE.match(buffer):
        sentence = match.group(1).strip()
        buffer = buffer[match.end():].lstrip()
        if sentence:
            sentences.append(sentence)
    if force and buffer.strip():
        sentences.append(buffer.strip())
        buffer = ""
    return sentences, buffer


class RioVoice:
    """Queue streamed sentences for OmniVoice and persistent PCM playback."""

    def __init__(self) -> None:
        self._buffer = ""
        self._closed = False
        self.audio_succeeded = False
        self.audio_failed = False
        self._tts_queue: queue.Queue[str | None] = queue.Queue()
        self._audio_queue: queue.Queue[bytes | None] = queue.Queue()
        self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self._audio_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self._tts_thread.start()
        self._audio_thread.start()

    def begin_utterance(self) -> None:
        """Called after wait() by the core presenter; settings remain unchanged."""
        self.audio_succeeded = False
        self.audio_failed = False

    def feed(self, token: str) -> None:
        if self._closed:
            raise RuntimeError("RioVoice is closed")
        sentences, self._buffer = pop_sentences(self._buffer + token)
        for sentence in sentences:
            self._tts_queue.put(sentence)

    def flush(self) -> None:
        if self._closed:
            return
        sentences, self._buffer = pop_sentences(self._buffer, force=True)
        for sentence in sentences:
            self._tts_queue.put(sentence)

    def wait(self) -> None:
        self._tts_queue.join()
        self._audio_queue.join()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self.wait()
        self._closed = True
        self._tts_queue.put(None)
        self._audio_queue.put(None)
        self._tts_thread.join()
        self._audio_thread.join()

    def __enter__(self) -> "RioVoice":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _tts_worker(self) -> None:
        with requests.Session() as session:
            while True:
                text = self._tts_queue.get()
                try:
                    if text is None:
                        return
                    spoken = clean_tts_text(text)
                    if not spoken:
                        continue
                    start = time.perf_counter()
                    response = session.post(
                        TTS_URL,
                        json={
                            "input": spoken,
                            "voice": "rio8",
                            "language": "Korean",
                            "response_format": "pcm",
                            "seed": 42,
                            "instructions": "korean accent",
                        },
                        timeout=120,
                    )
                    response.raise_for_status()
                    print(f"\n[TTS] {len(spoken)} chars / {time.perf_counter() - start:.3f}s")
                    self._audio_queue.put(response.content)
                except Exception as exc:
                    self.audio_failed = True
                    self.audio_succeeded = False
                    print(f"\n[TTS ERROR] {exc}")
                finally:
                    self._tts_queue.task_done()

    def _audio_worker(self) -> None:
        rio_eq = RioEQ(SAMPLE_RATE, RIO_EQ_BANDS)
        rio_deesser = RioDeEsser(
            sample_rate=SAMPLE_RATE,
            detect_freq=RIO_DEESSER_DETECT_FREQ,
            detect_q=RIO_DEESSER_DETECT_Q,
            highpass_freq=RIO_DEESSER_HIGHPASS_FREQ,
            threshold_db=RIO_DEESSER_THRESHOLD_DB,
            ratio=RIO_DEESSER_RATIO,
            max_reduction_db=RIO_DEESSER_MAX_REDUCTION_DB,
            attack_ms=RIO_DEESSER_ATTACK_MS,
            release_ms=RIO_DEESSER_RELEASE_MS,
        )
        try:
            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=0,
                latency="low",
            ) as stream:
                silence_samples = int(SAMPLE_RATE * PREBUFFER_MS / 1000)
                stream.write(b"\x00\x00" * silence_samples)
                while True:
                    pcm = self._audio_queue.get()
                    try:
                        if pcm is None:
                            return
                        if RIO_EQ_ENABLED:
                            pcm = rio_eq.process_pcm16(pcm)
                        if RIO_DEESSER_ENABLED:
                            pcm = rio_deesser.process_pcm16(pcm)
                        for pos in range(0, len(pcm), 2048):
                            block = pcm[pos:pos + 2048]
                            if len(block) & 1:
                                block = block[:-1]
                            if block:
                                stream.write(block)
                        self.audio_succeeded = bool(pcm) and not self.audio_failed
                    finally:
                        self._audio_queue.task_done()
        except Exception as exc:
            self.audio_failed = True
            self.audio_succeeded = False
            print(f"\n[AUDIO ERROR] {exc}")
            # Keep wait()/close() usable if the output device fails.
            while True:
                pcm = self._audio_queue.get()
                self._audio_queue.task_done()
                if pcm is None:
                    return

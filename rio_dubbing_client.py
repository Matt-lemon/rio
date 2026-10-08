"""Rio-side tool adapter. Job submission returns immediately; poll separately."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


class DubbingClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
        self.base_url = (base_url or os.getenv("RIO_DUBBING_URL", "http://127.0.0.1:8770")).rstrip("/")
        self.token = token if token is not None else os.getenv("RIO_DUBBING_TOKEN", "")

    def _request(self, method: str, path: str, payload: dict | None = None,
                 *, accept_unhealthy: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.request(method, self.base_url + path, json=payload, headers=headers)
            if not (accept_unhealthy and response.status_code == 503):
                response.raise_for_status()
            return response.json()

    def health(self) -> dict:
        """Keep the worker's diagnostic body on health503; mutations still fail."""
        value = self._request("GET", "/health", accept_unhealthy=True)
        if not isinstance(value, dict) or value.get("service") != "rio-dubbing":
            raise ValueError("unexpected dubbing health response")
        return value

    @staticmethod
    def _id(value: str) -> str:
        if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
            raise ValueError("invalid job/segment ID")
        return value

    def submit(self, source_path: str, *, source_language="zh", target_language="ko",
               mode="auto", speaker_mode="diarize", speakers=None, segments=None, background_path=None, export=None) -> dict:
        return self._request("POST", "/v1/jobs", {
            "source_path": source_path, "source_language": source_language,
            "target_language": target_language, "mode": mode, "speaker_mode": speaker_mode,
            "speakers": speakers or [], "segments": segments or [], "background_path": background_path,
            **({"export": export} if export is not None else {}),
        })

    def status(self, job_id: str) -> dict:
        return self._request("GET", f"/v1/jobs/{self._id(job_id)}")

    def retry(self, job_id: str) -> dict:
        return self._request("POST", f"/v1/jobs/{self._id(job_id)}/retry")

    def vary_speech(self, job_id: str, segment_id: str, *, expected_revision: int) -> dict:
        return self._request("POST", f"/v1/jobs/{self._id(job_id)}/segments/{self._id(segment_id)}/regenerate", {
            "expected_revision": expected_revision,
        })

    def start_dubbing(self, job_id: str, *, expected_revision: int) -> dict:
        return self._request("POST", f"/v1/jobs/{self._id(job_id)}/dub", {
            "expected_revision": expected_revision,
        })

    def export_video(self, job_id: str, *, expected_revision: int, export: dict) -> dict:
        return self._request("PATCH", f"/v1/jobs/{self._id(job_id)}/export", {
            "expected_revision": expected_revision, "export": export,
        })

    def regenerate(self, job_id: str, segment_id: str, translated_text: str,
                   *, emotion: str | None = None, input_audio_path: str | None = None,
                   expected_revision: int | None = None) -> dict:
        payload = {"translated_text": translated_text, "emotion": emotion, "input_audio_path": input_audio_path}
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        return self._request("PATCH", f"/v1/jobs/{self._id(job_id)}/segments/{self._id(segment_id)}", {
            **payload,
        })

    def correct_transcript(self, job_id: str, segment_id: str, source_text: str,
                           *, expected_revision: int) -> dict:
        return self._request("PATCH", f"/v1/jobs/{self._id(job_id)}/transcript/{self._id(segment_id)}", {
            "source_text": source_text, "expected_revision": expected_revision,
        })

    def artifact_url(self, job_id: str, kind="video") -> str:
        if kind not in {"video", "audio", "manifest", "dialogue_stem", "background_stem", "transcript", "subtitles", "target_subtitles", "speaker_analysis"}:
            raise ValueError("unknown artifact kind")
        return f"{self.base_url}/v1/jobs/{self._id(job_id)}/artifacts/{kind}"

    def dispatch(self, name: str, arguments: dict) -> dict:
        """Explicit callable bridge for Rio tool dispatchers."""
        handlers = {"dubbing_health": self.health, "dubbing_vary_speech": self.vary_speech,
                    "dubbing_submit": self.submit, "dubbing_status": self.status,
                    "dubbing_regenerate": self.regenerate, "dubbing_retry": self.retry,
                    "dubbing_correct_transcript": self.correct_transcript,
                    "dubbing_start": self.start_dubbing, "dubbing_export": self.export_video}
        if name not in handlers:
            raise ValueError("unknown dubbing tool")
        return handlers[name](**arguments)


def tool_definitions() -> list[dict]:
    """OpenAI-compatible function descriptors; registering these remains opt-in."""
    export_schema = {"type": "object", "additionalProperties": False, "properties": {
        "codec": {"type": "string", "enum": ["auto", "av1", "h264", "hevc", "copy"]},
        "container": {"type": "string", "enum": ["mp4", "matroska", "webm"]},
        "encoder": {"type": "string", "enum": ["auto", "amf", "software"]},
        "profile": {"type": "string", "enum": ["auto", "baseline", "main", "high", "main10"]},
        "level": {"type": "string"},
        "quality": {"type": "string", "enum": ["speed", "balanced", "quality", "high_quality"]},
        "rate_control": {"type": "string", "enum": ["vbr_peak", "cqp"]},
        "bitrate_kbps": {"type": "integer", "minimum": 100, "maximum": 200000},
        "quality_value": {"type": "integer", "minimum": 0, "maximum": 255},
        "max_size_mb": {"type": "number", "minimum": 1, "maximum": 100000},
        "audio_codec": {"type": "string", "enum": ["auto", "aac", "opus"]},
        "audio_bitrate_kbps": {"type": "integer", "minimum": 16, "maximum": 512},
        "audio_channels": {"type": "string", "enum": ["auto", "mono", "stereo"]},
        "audio_sample_rate": {"type": "string", "enum": ["auto", "44100", "48000"]}}}
    specs = [
        ("dubbing_health", "Check API worker health before maintenance. Provider flags mean configured, not model connectivity. Unhealthy workers require operator inspection, not repeated job retries.",
         {}, []),
        ("dubbing_vary_speech", "Generate a different voice sample for one segment without changing reviewed text. Requires latest revision; listen before accepting. Ordinary retry keeps the previous seed.",
         {"job_id": {"type": "string"}, "segment_id": {"type": "string"},
          "expected_revision": {"type": "integer", "minimum": 1}},
         ["job_id", "segment_id", "expected_revision"]),
        ("dubbing_submit", "Submit a local dubbing job; returns a job ID immediately. Engines may be blocked.",
         {"source_path": {"type": "string"}, "source_language": {"type": "string", "default": "zh"},
          "target_language": {"type": "string", "default": "ko"},
          "mode": {"type": "string", "enum": ["auto", "analyze"], "default": "auto"},
          "speaker_mode": {"type": "string", "enum": ["diarize", "single"], "default": "diarize"},
          "export": export_schema}, ["source_path"]),
        ("dubbing_status", "Check status. completed means rendered output; analysis_ready means transcript only; preview_ready means demo; blocked needs an engine.",
         {"job_id": {"type": "string"}}, ["job_id"]),
        ("dubbing_regenerate", "Change one segment and queue a revision; other clips are cached. Render mode also needs a new WAV.",
         {"job_id": {"type": "string"}, "segment_id": {"type": "string"},
          "translated_text": {"type": "string"}, "emotion": {"type": "string"},
          "expected_revision": {"type": "integer", "minimum": 1},
          "input_audio_path": {"type": "string"}}, ["job_id", "segment_id", "translated_text"]),
        ("dubbing_retry", "Explicitly retry a blocked or failed job after resolving the cause.",
         {"job_id": {"type": "string"}}, ["job_id"]),
        ("dubbing_start", "Continue a reviewed analysis_ready job into Korean dubbing, preserving source corrections and cached analysis.",
         {"job_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1}},
         ["job_id", "expected_revision"]),
        ("dubbing_export", "Re-encode only the video using saved dubbed audio. Default is one AV1 MP4; other formats are generated on request. max_size_mb=50 requires a file strictly below 50,000,000 bytes.",
         {"job_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1},
          "export": export_schema}, ["job_id", "expected_revision", "export"]),
        ("dubbing_correct_transcript", "Correct one source-text segment in an analysis_ready analyze job; preserves cached inference. Corrected words need realignment.",
         {"job_id": {"type": "string"}, "segment_id": {"type": "string"},
          "source_text": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1}},
         ["job_id", "segment_id", "source_text", "expected_revision"]),
    ]
    return [{"type": "function", "function": {"name": name, "description": description,
             "parameters": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}}
            for name, description, properties, required in specs]

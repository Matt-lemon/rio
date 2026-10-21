"""Small local Spotify adapter: explicit Play/Pause, no toggle or cloud credentials."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time


PLAYER = "org.mpris.MediaPlayer2.spotify"
OBJECT = "/org/mpris/MediaPlayer2"
ROOT_INTERFACE = "org.mpris.MediaPlayer2"
INTERFACE = ROOT_INTERFACE + ".Player"


class LocalSpotify:
    def _run(self, *arguments: str) -> str:
        runtime = f"/run/user/{os.getuid()}"
        result = subprocess.run(
            ["/usr/bin/busctl", "--user", "--json=short", "--timeout=3", *arguments],
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": runtime,
                 "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"},
            capture_output=True, text=True, timeout=4, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 8192:
            raise ValueError("Local Spotify unavailable")
        return result.stdout

    def _properties(self, owner: str, interface: str, names: tuple[str, ...],
                    types: tuple[type, ...]) -> list:
        lines = self._run("get-property", owner, OBJECT, interface, *names).splitlines()
        if len(lines) != len(names):
            raise ValueError("Invalid Spotify properties")
        values = []
        for line, expected in zip(lines, types):
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("Invalid Spotify property")
            value = item.get("data")
            if item.get("type") != ("b" if expected is bool else "s") or type(value) is not expected:
                raise ValueError("Invalid Spotify property type")
            values.append(value)
        return values

    def snapshot(self) -> dict:
        response = json.loads(self._run("call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                        "org.freedesktop.DBus", "GetNameOwner", "s", PLAYER))
        if not isinstance(response, dict):
            raise ValueError("Invalid Spotify owner")
        data = response.get("data")
        if (response.get("type") != "s" or not isinstance(data, list) or len(data) != 1
                or not isinstance(data[0], str) or not re.fullmatch(r":\d+\.\d+", data[0])):
            raise ValueError("Spotify owner unavailable")
        owner = data[0]
        identity, entry = self._properties(owner, ROOT_INTERFACE, ("Identity", "DesktopEntry"), (str, str))
        if identity != "Spotify" or entry != "spotify":
            raise ValueError("Unexpected player")
        state, control, play, pause = self._properties(
            owner, INTERFACE, ("PlaybackStatus", "CanControl", "CanPlay", "CanPause"),
            (str, bool, bool, bool),
        )
        if state not in {"Playing", "Paused", "Stopped"}:
            raise ValueError("Unknown playback status")
        return {"owner": owner, "playback": state, "can_control": control,
                "can_play": play, "can_pause": pause}

    def command(self, owner: str, action: str) -> None:
        # The destination is the same unique process that supplied the capabilities.
        if action not in {"play", "pause"} or not re.fullmatch(r":\d+\.\d+", owner):
            raise ValueError("Unsupported Spotify action")
        self._run("call", owner, OBJECT, INTERFACE, {"play": "Play", "pause": "Pause"}[action])


class SpotifyController:
    def __init__(self, transport=None, sleep=time.sleep):
        self.transport = transport or LocalSpotify()
        self.sleep = sleep

    def call(self, action: str, before_write=None) -> dict:
        if action not in {"status", "play", "pause"}:
            return {"status": "REJECTED", "code": "UNSUPPORTED_ACTION"}
        try:
            before = self.transport.snapshot()
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"status": "UNAVAILABLE", "code": "LOCAL_SPOTIFY_UNAVAILABLE"}
        if action == "status":
            return {"status": "OBSERVED", "playback": before["playback"]}
        desired = "Playing" if action == "play" else "Paused"
        already = before["playback"] == desired or (action == "pause" and before["playback"] == "Stopped")
        if not already and (not before["can_control"] or not before["can_" + action]):
            return {"status": "REJECTED", "code": "PLAYER_CANNOT_CONTROL"}
        if before_write is not None and before_write().get("status") != "SCHEDULED":
            return {"status": "REJECTED", "code": "TIMER_NOT_ARMED"}
        if already:
            return {"status": "SUCCEEDED", "playback": before["playback"], "command_sent": False}
        try:
            self.transport.command(before["owner"], action)
        except (OSError, ValueError, subprocess.SubprocessError):
            # A timeout may occur after delivery. Never repeat a write or use a toggle.
            return {"status": "UNKNOWN", "code": "DELIVERY_UNCERTAIN"}
        for attempt in range(3):
            if attempt:
                self.sleep(0.15)
            try:
                after = self.transport.snapshot()
            except (OSError, ValueError, subprocess.SubprocessError):
                break
            if after["owner"] != before["owner"]:
                break
            if after["playback"] == desired:
                return {"status": "SUCCEEDED", "playback": desired, "command_sent": True}
        return {"status": "UNKNOWN", "code": "PLAYBACK_NOT_CONFIRMED"}


def parse_spotify_request(text: str) -> str | None:
    text = re.sub(r"^(?:리오야|리오)[,，]?\s+", "", text.strip())
    direct = re.fullmatch(r"/spotify\s+(status|play|pause)", text, re.IGNORECASE)
    if direct:
        return direct[1].lower()
    # Full utterances only: quoted, negated, conditional and compound requests do not match.
    if re.search(r"(?:줘|주세요|줄래(?:요)?)[?？]$", text):
        text = text[:-1]
    text = re.sub(r"[.!。！]$", "", text).strip()
    target = r"(?:(?:스포티파이|spotify|음악|노래)(?:를|을)?\s*)?"
    filler = r"(?:좀\s*)?"
    ending = r"(?:\s*(?:줘|주세요|줄래(?:요)?|주라|봐(?:줘)?|부탁해(?:요)?))?"
    play = r"(?:(?:다시|계속|이어서)\s*)?(?:재생(?:\s*해)?|틀어)"
    pause = r"(?:(?:잠깐|잠시)\s*)?(?:일시\s*정지(?:\s*해)?|멈춰)"
    if re.fullmatch(target + filler + play + ending, text, re.IGNORECASE):
        return "play"
    if re.fullmatch(target + filler + pause + ending, text, re.IGNORECASE):
        return "pause"
    # Power-like verbs require an explicit music target, never bare '켜줘/꺼줘'.
    explicit = re.fullmatch(r"(?:스포티파이|spotify|음악|노래)(?:를|을)?\s*(?:좀\s*)?(켜|꺼)" + ending,
                            text, re.IGNORECASE)
    if explicit:
        return "play" if explicit[1] == "켜" else "pause"
    if re.fullmatch(r"(?:스포티파이|spotify|음악|노래)\s*상태(?:\s*알려" + ending + r")?", text, re.IGNORECASE):
        return "status"
    return None


def handle_spotify_command(text: str, factory=None) -> str | None:
    from .spotify_timer import handle_timer_command
    timer_reply = handle_timer_command(text, controller_factory=factory)
    if timer_reply is not None:
        return timer_reply
    action = parse_spotify_request(text)
    if action is None:
        if re.match(r"^/spotify(?:\s|$)", text.strip(), re.IGNORECASE):
            return "음악 명령: 음악 틀어줘 · 일시정지해줘 · 다시 재생해줘 · /spotify status"
        return None
    answer = (factory or SpotifyController)().call(action)
    if answer["status"] in {"SUCCEEDED", "OBSERVED"}:
        state = {"Playing": "재생 중", "Paused": "일시정지", "Stopped": "정지"}[answer["playback"]]
        return f"Spotify가 {state} 상태라고 보고했어요."
    if answer["status"] == "UNKNOWN":
        return "음악 요청 결과를 확인하지 못했어요. 자동으로 다시 보내지 않을게요."
    if answer["status"] == "UNAVAILABLE":
        return "NAS의 Spotify 연결을 확인하지 못했어요. 앱 실행·로그인과 리오 서비스 접속을 확인해 주세요."
    return "Spotify가 지금은 해당 동작을 지원하지 않아 실행하지 않았어요."

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from web_search import web_search

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "agency.yaml"
DATA_DIR = ROOT / "data" / "agency"

load_dotenv(ROOT / ".env")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "local-model")
LLM_API_KEY = os.getenv("LLM_API_KEY", "not-needed")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


class RioAgent:
    def __init__(self) -> None:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        self.cfg = raw if isinstance(raw, dict) else {}
        self.enabled = str(
            os.getenv("RIO_AGENCY_ENABLED", self.cfg.get("enabled", True))
        ).lower() in {"1", "true", "yes", "on"}
        self.tick_seconds = max(
            10,
            int(os.getenv("RIO_AGENCY_TICK_SECONDS", self.cfg.get("tick_seconds", 60))),
        )
        self.allowed = set(
            self.cfg.get("allowed_actions")
            or ["noop", "note", "notify", "web_search"]
        )
        self.max_actions_per_hour = max(
            1,
            int(self.cfg.get("max_actions_per_hour", 8)),
        )
        self.max_web_search_per_day = max(
            0,
            int(self.cfg.get("max_web_search_per_day", 20)),
        )
        self.goals = [str(x) for x in (self.cfg.get("goals") or [])]

        self.inbox = DATA_DIR / "inbox.jsonl"
        self.outbox = DATA_DIR / "outbox.jsonl"
        self.notes = DATA_DIR / "notes.jsonl"
        self.actions = DATA_DIR / "actions.jsonl"
        self.state_path = DATA_DIR / "state.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, text: str) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex,
            "ts": _now(),
            "kind": kind[:80],
            "text": text[:4000],
        }
        _append_jsonl(self.inbox, item)
        return item

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"processed": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"processed": []}
        return data if isinstance(data, dict) else {"processed": []}

    def _save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

    def _pending_events(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        done = set(str(x) for x in state.get("processed", []))
        return [
            x
            for x in _read_jsonl(self.inbox, 500)
            if str(x.get("id", "")) not in done
        ][-20:]

    def _mark_processed(
        self,
        state: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        done = [str(x) for x in state.get("processed", [])]
        done.extend(str(x.get("id")) for x in events if x.get("id"))
        state["processed"] = list(dict.fromkeys(done))[-1000:]
        self._save_state(state)

    def _recent_actions(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.actions, 500)

    @staticmethod
    def _timestamp(item: dict[str, Any]) -> float:
        try:
            return datetime.fromisoformat(str(item.get("ts"))).timestamp()
        except ValueError:
            return 0.0

    def _budget_allows(
        self,
        action: str,
        recent: list[dict[str, Any]],
    ) -> bool:
        now = time.time()
        hourly = sum(
            1
            for x in recent
            if x.get("action") != "noop"
            and self._timestamp(x) >= now - 3600
        )
        if action != "noop" and hourly >= self.max_actions_per_hour:
            return False

        if action == "web_search":
            today = datetime.now().astimezone().date()
            searches = 0
            for x in recent:
                if x.get("action") != "web_search":
                    continue
                try:
                    stamp = datetime.fromisoformat(
                        str(x.get("ts"))
                    ).astimezone().date()
                except ValueError:
                    continue
                if stamp == today:
                    searches += 1

            if searches >= self.max_web_search_per_day:
                return False

        return True

    def _plan(
        self,
        events: list[dict[str, Any]],
        recent: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not events:
            return {
                "action": "noop",
                "reason": "새 이벤트 없음",
                "text": "",
                "query": "",
            }

        system = """너는 Rio의 자율 행동 선택기다.
제공된 이벤트에 근거해 이번 tick에서 최대 하나만 행동한다.
외부 상태를 추측하지 않고, 하지 않은 일을 했다고 주장하지 않는다.
의미 있는 변화가 없으면 noop.
notify는 사용자가 지금 알아야 할 변화에만 쓴다.
web_search는 최신 정보가 실제로 필요할 때만 쓴다.
허용된 행동 외의 쉘 실행, 파일 수정/삭제, 전원 제어, 결제, 외부 메시지 전송은 하지 않는다.
숨은 사고과정은 쓰지 말고 짧은 reason만 쓴다.
JSON 하나만 출력:
{"action":"noop|note|notify|web_search","reason":"120자 이하","text":"","query":""}"""

        context = {
            "goals": self.goals,
            "allowed_actions": sorted(self.allowed),
            "time": _now(),
            "events": events,
            "recent_actions": recent[-8:],
        }

        with httpx.Client(timeout=min(LLM_TIMEOUT, 30)) as client:
            response = client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": json.dumps(
                                context,
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": 180,
                    "stream": False,
                    "reasoning_effort": "none",
                    "chat_template_kwargs": {
                        "enable_thinking": False,
                    },
                },
            )
            response.raise_for_status()

        content = str(
            response.json()["choices"][0]["message"]["content"]
        )
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return {
                "action": "noop",
                "reason": "planner JSON 없음",
                "text": "",
                "query": "",
            }

        try:
            result = json.loads(match.group())
        except json.JSONDecodeError:
            return {
                "action": "noop",
                "reason": "planner JSON 파싱 실패",
                "text": "",
                "query": "",
            }

        if not isinstance(result, dict):
            return {
                "action": "noop",
                "reason": "planner 형식 오류",
                "text": "",
                "query": "",
            }

        return result

    def _execute(
        self,
        proposal: dict[str, Any],
        recent: list[dict[str, Any]],
    ) -> dict[str, Any]:
        action = str(proposal.get("action", "noop")).strip()
        reason = str(proposal.get("reason", "")).strip()[:160]

        if action not in self.allowed:
            blocked = action
            action = "noop"
            reason = f"허용되지 않은 행동 차단: {blocked}"

        if not self._budget_allows(action, recent):
            action = "noop"
            reason = "자율 행동 예산 초과"

        result: dict[str, Any] = {
            "ts": _now(),
            "action": action,
            "reason": reason,
            "ok": True,
        }

        if action == "note":
            text = str(proposal.get("text", "")).strip()
            if text:
                _append_jsonl(
                    self.notes,
                    {"ts": _now(), "text": text[:2000]},
                )
                result["text"] = text[:500]
            else:
                result.update(
                    action="noop",
                    reason="빈 note 차단",
                )

        elif action == "notify":
            text = str(proposal.get("text", "")).strip()
            if text:
                _append_jsonl(
                    self.outbox,
                    {"ts": _now(), "text": text[:2000]},
                )
                print(f"[Rio autonomous] {text}")
                result["text"] = text[:500]
            else:
                result.update(
                    action="noop",
                    reason="빈 notify 차단",
                )

        elif action == "web_search":
            query = str(
                proposal.get("query", "")
            ).strip()[:200]

            if not query:
                result.update(
                    action="noop",
                    reason="빈 검색어 차단",
                )
            else:
                found = web_search(
                    query,
                    max_results=4,
                    timeout=10,
                )
                self.emit(
                    "web_search_result",
                    json.dumps(
                        {
                            "query": query,
                            "results": [
                                {
                                    "title": x.title,
                                    "url": x.url,
                                    "snippet": x.snippet,
                                }
                                for x in found
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
                result["query"] = query
                result["results"] = len(found)

        _append_jsonl(self.actions, result)
        return result

    def run_once(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        state = self._load_state()
        events = self._pending_events(state)
        recent = self._recent_actions()

        if not self.enabled:
            result = {
                "ts": _now(),
                "action": "noop",
                "reason": "agency disabled",
                "ok": True,
            }
            _append_jsonl(self.actions, result)
            return result

        try:
            proposal = self._plan(events, recent)
        except Exception as exc:
            proposal = {
                "action": "noop",
                "reason": f"planner 실패: {type(exc).__name__}",
                "text": "",
                "query": "",
            }

        if dry_run:
            result = {
                "ts": _now(),
                "dry_run": True,
                "proposal": proposal,
            }
        else:
            try:
                result = self._execute(
                    proposal,
                    recent,
                )
            except Exception as exc:
                result = {
                    "ts": _now(),
                    "action": "noop",
                    "reason": "action 실행 실패",
                    "ok": False,
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    )[:500],
                }
                _append_jsonl(
                    self.actions,
                    result,
                )

        self._mark_processed(
            state,
            events,
        )
        return result

    def run_forever(self) -> None:
        while True:
            print(
                json.dumps(
                    self.run_once(),
                    ensure_ascii=False,
                )
            )
            time.sleep(self.tick_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rio bounded autonomous action loop"
    )
    parser.add_argument(
        "--once",
        action="store_true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
    )
    parser.add_argument(
        "--event",
        nargs=2,
        metavar=("KIND", "TEXT"),
    )
    args = parser.parse_args()

    agent = RioAgent()

    if args.event:
        print(
            json.dumps(
                agent.emit(
                    args.event[0],
                    args.event[1],
                ),
                ensure_ascii=False,
            )
        )
        return

    if args.once or args.dry_run:
        print(
            json.dumps(
                agent.run_once(
                    dry_run=args.dry_run,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    agent.run_forever()


if __name__ == "__main__":
    main()

"""Persistent, wall-clock Spotify pause timers. No replay after uncertain delivery."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo

from .spotify import SpotifyController

KST = ZoneInfo("Asia/Seoul")
MAX_DELAY = 7 * 86400
SERVICE = "rio-spotify-timer.service"


class TimerParseError(ValueError):
    pass


def duration_seconds(text: str) -> int:
    text = text.replace('반시간', '30분').replace('시간 반', '시간 30분').replace('시간반', '시간30분')
    numbers = {'한': '1', '두': '2', '세': '3', '네': '4', '십': '10', '이십': '20',
               '삼십': '30', '사십': '40', '오십': '50'}
    for word in sorted(numbers, key=len, reverse=True):
        text = re.sub(word + r'(?=\s*(?:시간|분|초))', numbers[word], text)
    text = re.sub(r'\s+', '', text)
    match = re.fullmatch(r'(?:(\d+)시간)?(?:(\d+)분)?(?:(\d+)초)?', text)
    if not match or not any(match.groups()):
        raise TimerParseError('BAD_DURATION')
    seconds = sum(int(value or 0) * factor for value, factor in zip(match.groups(), [3600, 60, 1]))
    if not 1 <= seconds <= MAX_DELAY:
        raise TimerParseError('BAD_DURATION')
    return seconds


def parse_timer_request(text: str, now: datetime | None = None) -> dict | None:
    now = (now or datetime.now(KST)).astimezone(KST)
    text = re.sub(r'^(?:리오야|리오)[,，]?\s+', '', text.strip())
    text = re.sub(r'[.!。！]$', '', text).strip()
    direct = re.fullmatch(r'/spotify\s+timer\s+(status|cancel|after\s+\d+[smh])', text, re.I)
    if direct:
        action = direct[1].lower()
        if action in {'status', 'cancel'}:
            return {'kind': action}
        count, unit = re.fullmatch(r'after\s+(\d+)([smh])', action).groups()
        seconds = int(count) * {'s': 1, 'm': 60, 'h': 3600}[unit]
        if not 1 <= seconds <= MAX_DELAY:
            raise TimerParseError('BAD_DURATION')
        return {'kind': 'schedule', 'due': now.timestamp() + seconds, 'play': False}
    # Removing a music target does not remove other devices, quotes or conditions.
    text = re.sub(r'(?:스포티파이|spotify|음악|노래)(?:을|를)?\s*', '', text, flags=re.I).strip()
    if re.fullmatch(r'(?:타이머|예약)\s*취소(?:해)?\s*(?:줘|주세요)?', text):
        return {'kind': 'cancel'}
    if re.fullmatch(r'(?:타이머|예약)\s*(?:상태(?:\s*알려(?:줘|주세요)?)?|확인(?:해줘)?|남은\s*시간(?:\s*알려줘)?|얼마나\s*남았어[?？]?)', text):
        return {'kind': 'status'}
    if re.fullmatch(r'(?:얼마나|몇\s*분)\s*남았어[?？]?', text):
        return None  # No bare context-dependent question is commandeered as a music request.
    ending = r'\s*(?:줘|주세요|줄래(?:요)?|부탁해(?:요)?)?'
    stop = r'(?:좀\s*)?(?:멈춰|꺼|일시\s*정지(?:\s*(?:해|시켜))?)' + ending
    play = r'(?:좀\s*)?(?:틀어|재생(?:\s*해)?)' + ending
    relative = re.fullmatch(r'(.+?)\s*(?:뒤|후)(?:에)?\s*' + stop, text)
    if relative:
        seconds = duration_seconds(relative[1])
        return {'kind': 'schedule', 'due': now.timestamp() + seconds, 'play': False}
    limited = re.fullmatch(r'(.+?)\s*(?:(?<!까지)만|동안)\s*' + play, text)
    if limited:
        seconds = duration_seconds(limited[1])
        return {'kind': 'schedule', 'due': now.timestamp() + seconds, 'play': True}
    clock = r'(?:(오늘|내일|모레)\s*)?(?:(오전|오후|아침|저녁|밤|새벽|낮)\s*)?(\d{1,2})(?:(?::(\d{2}))|시(?:\s*(반|\d{1,2}\s*분))?)'
    absolute = re.fullmatch(clock + r'\s*(까지만|까지|에)\s*(.*)', text)
    if not absolute:
        return None
    day, period, hour_text, colon_minute, named_minute, until, tail = absolute.groups()
    is_play = bool(re.fullmatch(play, tail)) if tail else False
    if not (re.fullmatch(stop, tail) or is_play or (not tail and until in {'까지', '까지만'})):
        return None
    hour = int(hour_text)
    minute = int(colon_minute) if colon_minute else 30 if named_minute == '반' else int(re.sub(r'\D', '', named_minute) or 0) if named_minute else 0
    if minute > 59 or hour > 23:
        raise TimerParseError('BAD_CLOCK')
    midnight_after_night = False
    if period:
        if not 1 <= hour <= 12:
            raise TimerParseError('BAD_CLOCK')
        hour %= 12
        if period in {'오후', '저녁', '밤', '낮'}:
            if period == '밤' and hour == 0:
                midnight_after_night = True
            else:
                hour += 12
    elif colon_minute is None and 1 <= hour <= 12:
        raise TimerParseError('AMBIGUOUS_CLOCK')
    date = now.date() + timedelta(days={'오늘': 0, '내일': 1, '모레': 2, None: 0}[day])
    if midnight_after_night:
        date += timedelta(days=1)
    due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=KST)
    if due <= now:
        raise TimerParseError('PAST_CLOCK')
    return {'kind': 'schedule', 'due': due.timestamp(), 'play': is_play}


def worker_active() -> bool:
    try:
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'is-active', '--quiet', SERVICE],
                                capture_output=True, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class TimerStore:
    def __init__(self, directory: Path | None = None, clock=time.time, active=worker_active):
        self.directory = directory or Path.home()/'.local/state/rio/spotify'
        self.clock, self.active = clock, active

    @contextmanager
    def database(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Private timer directory required')
        path = self.directory/'timers.sqlite3'
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError('Private timer database required')
        finally:
            os.close(fd)
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('CREATE TABLE IF NOT EXISTS timers (id TEXT PRIMARY KEY, due REAL NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, result TEXT)')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_timer ON timers ((1)) WHERE state IN ('ARMED','EXECUTING')")
            db.commit()
            yield db
        finally:
            db.close()

    def schedule(self, due: float, minimum_delay: float = 0) -> dict:
        now = self.clock()
        if not isinstance(due, (int, float)) or not math.isfinite(due) or not minimum_delay < due-now <= MAX_DELAY:
            return {'status': 'REJECTED', 'code': 'INVALID_DEADLINE'}
        if not self.active():
            return {'status': 'REJECTED', 'code': 'TIMER_WORKER_UNAVAILABLE'}
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            if not minimum_delay < due-now <= MAX_DELAY:
                db.rollback()
                return {'status': 'REJECTED', 'code': 'INVALID_DEADLINE'}
            if db.execute("SELECT 1 FROM timers WHERE state='EXECUTING'").fetchone():
                db.rollback()
                return {'status': 'REJECTED', 'code': 'TIMER_ALREADY_EXECUTING'}
            replaced = db.execute("UPDATE timers SET state='REPLACED',updated=? WHERE state='ARMED'", (now,)).rowcount
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO timers VALUES (?,?,?,?,?,NULL)', (ident, due, 'ARMED', now, now))
            db.commit()
        return {'status': 'SCHEDULED', 'id': ident, 'due': due, 'replaced': bool(replaced)}

    def status(self) -> dict:
        with self.database() as db:
            row = db.execute('SELECT id,due,state,result FROM timers ORDER BY created DESC, rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else {'state': 'NONE'}

    def cancel(self) -> dict:
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM timers WHERE state='EXECUTING'").fetchone():
                db.rollback()
                return {'state': 'EXECUTING'}
            count = db.execute("UPDATE timers SET state='CANCELLED',updated=? WHERE state='ARMED'", (self.clock(),)).rowcount
            db.commit()
        return {'state': 'CANCELLED' if count else 'NONE'}

    def recover(self):
        # Mark interrupted writes unknown; neither restart nor a second process may replay them.
        with self.database() as db:
            db.execute("UPDATE timers SET state='UNKNOWN',updated=?,result=? WHERE state='EXECUTING'",
                       (self.clock(), json.dumps({'code': 'WORKER_INTERRUPTED'})))
            db.commit()

    def run_due(self, controller=None) -> dict | None:
        now = self.clock()
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM timers WHERE state='ARMED' AND due<=?", (now,)).fetchone()
            if row is None:
                db.rollback()
                return None
            if now-row['due'] > 60:
                db.execute("UPDATE timers SET state='MISSED',updated=? WHERE id=?", (now, row['id']))
                db.commit()
                return {'state': 'MISSED'}
            db.execute("UPDATE timers SET state='EXECUTING',updated=? WHERE id=?", (now, row['id']))
            db.commit()
        answer = (controller or SpotifyController()).call('pause')
        final = 'FINISHED' if answer.get('status') == 'SUCCEEDED' else 'UNKNOWN'
        with self.database() as db:
            db.execute("UPDATE timers SET state=?,updated=?,result=? WHERE id=? AND state='EXECUTING'",
                       (final, self.clock(), json.dumps(answer), row['id']))
            db.commit()
        return {'state': final, 'result': answer}


def format_deadline(due: float) -> str:
    return datetime.fromtimestamp(due, KST).strftime('%Y-%m-%d %H:%M:%S (한국시간)')


def handle_timer_command(text: str, store_factory=None, controller_factory=None, now=None) -> str | None:
    try:
        request = parse_timer_request(text, now)
    except TimerParseError as error:
        return {'AMBIGUOUS_CLOCK': '오전·오후를 알려주세요. 18:30처럼 24시간으로 말해도 돼요.',
                'PAST_CLOCK': '이미 지난 시각이에요. 다음 날로 넘기지 않았어요. 내일이면 날짜를 함께 알려주세요.'}.get(str(error),
                '시간을 확인해 주세요. 예: 30분 뒤, 오늘 23:00, 내일 오전 7시.')
    if request is None:
        return None
    try:
        store = (store_factory or TimerStore)()
        if request['kind'] == 'cancel':
            result = store.cancel()
            return {'CANCELLED': '음악 정지 타이머를 취소했어요.',
                    'EXECUTING': '타이머가 이미 실행 중이라 취소 완료로 처리하지 않았어요.'}.get(result['state'], '예약된 음악 정지 타이머가 없어요.')
        if request['kind'] == 'status':
            result = store.status()
            if result['state'] == 'ARMED':
                remaining = max(0, math.ceil(result['due']-store.clock()))
                availability = '' if store.active() else ' 예약 실행 서비스는 현재 중지돼 있어요.'
                return f"음악은 {format_deadline(result['due'])}에 일시정지 예정이에요. 약 {remaining//60}분 {remaining%60}초 남았어요." + availability
            return {'FINISHED': '음악 정지 타이머가 완료됐어요.', 'UNKNOWN': '이전 타이머 결과를 확인하지 못했어요. 자동 재시도하지 않아요.',
                    'EXECUTING': '음악 정지 타이머가 실행 중이에요.', 'MISSED': '실행 시각을 놓친 타이머예요. 늦은 제어는 보내지 않았어요.',
                    'CANCELLED': '음악 정지 타이머는 취소된 상태예요.'}.get(result['state'], '예약된 음악 정지 타이머가 없어요.')
        outcome = {}
        def arm():
            # Arm only after Spotify preflight, and before a requested Play can be sent.
            outcome.update(store.schedule(request['due'], minimum_delay=10 if request['play'] else 0))
            return outcome
        playback = None
        if request['play']:
            playback = (controller_factory or SpotifyController)().call('play', before_write=arm)
        else:
            arm()
        if outcome.get('status') != 'SCHEDULED':
            return '음악 정지 타이머를 등록하지 못했어요. 실행 서비스와 시각을 확인해 주세요. 재생도 새로 시작하지 않았어요.'
        message = f"음악을 {format_deadline(outcome['due'])}에 일시정지하도록 예약했어요."
        if outcome.get('replaced'):
            message += ' 이전 음악 타이머는 교체했어요.'
        if playback is not None:
            message += ' 재생 상태도 확인했어요.' if playback.get('status') == 'SUCCEEDED' else ' 재생 결과는 확인하지 못했지만 정지 예약은 유지해요.'
        return message
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return '음악 타이머 상태를 확인하지 못했어요. 예약·취소 완료로 처리하거나 제어를 다시 보내지 않을게요.'

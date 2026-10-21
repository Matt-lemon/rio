"""Bounded suggestions, sourced from actual tasks; never an execution authority."""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .types import encoded, fingerprint, safe_text, SENSITIVE


class Suggestions:
    def __init__(self, store, search=None):
        self.store = store
        if search is None:
            from web_search import web_search
            search = lambda query: web_search(query, max_results=4, timeout=10)
        self.search = search

    def add(self, task_id, text, evidence):
        key = fingerprint({'task': task_id, 'text': text, 'evidence': evidence})
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO suggestions VALUES(?,?,?,?,?,NULL)',
                       (key, task_id, safe_text(text, 240), encoded(evidence), self.store.clock()))
        return key

    def ingest(self, kind, text, event_id):
        # Legacy agency/event output is evidence for a suggestion, never a task.
        if not isinstance(event_id, str) or len(event_id) > 100 or not isinstance(text, str) or SENSITIVE.search(text):
            raise ValueError('INVALID_AGENCY_EVENT')
        key = fingerprint({'legacy_event_id': event_id})
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO suggestions VALUES(?,?,?,?,?,NULL)',
                       (key, 'event:' + event_id, '새 작업 참고 사항: ' + safe_text(text, 180),
                        encoded({'kind': safe_text(kind, 80), 'source': 'legacy_agency_event'}), self.store.clock()))
        return key

    def tick(self):
        now = self.store.clock()
        with self.store.transaction() as db:
            marker = db.execute("SELECT value FROM meta WHERE key='suggestion_tick'").fetchone()
            if marker and now - float(marker['value']) < 1800:
                return
            db.execute("INSERT INTO meta VALUES('suggestion_tick',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
        self.import_legacy_events()
        tasks = self.store.rows("SELECT * FROM tasks WHERE state IN ('WAITING_INPUT','FAILED','UNKNOWN','PAUSED','COMPLETED') ORDER BY updated DESC LIMIT 100")
        for task in tasks:
            if task['state'] == 'COMPLETED' and now - task['updated'] > 7 * 86400:
                continue
            calls = self.store.calls(task['id'])
            if calls:
                # Local task topics need no external disclosure. Public research
                # is restricted below to known public queries/component names.
                topic = '작업: ' + safe_text(task['request'], 120)
                key = fingerprint({'task': task['id'], 'source': 'task_goal'})
                with self.store.transaction() as db:
                    db.execute('INSERT OR IGNORE INTO interests(id,task_id,topic,source,signature,created,checked) VALUES(?,?,?,?,?,?,?)',
                               (key, task['id'], topic, 'task_goal_local_only', fingerprint(task['request']), now, -1))
            if task['state'] in {'WAITING_INPUT', 'FAILED', 'UNKNOWN'}:
                self.add(task['id'], '이전에 멈춘 작업이 있어. 원하면 저장된 결과를 확인하고 다음 단계를 정리할게.',
                         {'state': task['state'], 'source_task': task['id']})
            for call in calls:
                docs = {'home': 'Google Home Android API 기기 제어 공식 문서',
                        'browser': 'Playwright 브라우저 연결 공식 문서',
                        'registry': 'MCP 도구 스키마 공식 문서',
                        'spotify': 'MPRIS MediaPlayer2 Player 공식 문서'}
                component = call['name'].split('.')[0]
                if call['state'] in {'UNKNOWN', 'UNAVAILABLE', 'REJECTED'} and component in docs:
                    key = fingerprint({'task': task['id'], 'component': component})
                    with self.store.transaction() as db:
                        db.execute('INSERT OR IGNORE INTO interests(id,task_id,topic,source,signature,created) VALUES(?,?,?,?,?,?)',
                                   (key, task['id'], docs[component], 'component_documentation', fingerprint({'state': call['state']}), now))
                # Only public queries already issued by the user/task seed
                # external research. Private device labels/errors never do.
                if call['name'] not in {'web.search', 'browser.search', 'browser.open_topic'} or not call['result']:
                    continue
                query = call['args'].get('query', '')
                if not isinstance(query, str) or not 1 <= len(query) <= 160 or SENSITIVE.search(query) or re.search(r'https?://|[\\/]|\b\d{6,}\b', query):
                    continue
                key = fingerprint({'task': task['id'], 'query': query})
                with self.store.transaction() as db:
                    db.execute('INSERT OR IGNORE INTO interests(id,task_id,topic,source,signature,created) VALUES(?,?,?,?,?,?)',
                               (key, task['id'], query, 'issued_public_query', fingerprint(call['result']), now))
        day = datetime.fromtimestamp(now, ZoneInfo('Asia/Seoul')).date().isoformat()
        for item in self.store.rows('SELECT * FROM interests WHERE excluded=0 AND checked=0 ORDER BY created LIMIT 10'):
            task = self.store.task(item['task_id'])
            if not item['kept'] and task['state'] in {'COMPLETED', 'CANCELLED'} and now - task['updated'] > 7 * 86400:
                continue
            # Reserve quota before network I/O; interruption cannot refund it.
            with self.store.transaction() as db:
                current = db.execute('SELECT checked,excluded FROM interests WHERE id=?', (item['id'],)).fetchone()
                if not current or current['checked'] or current['excluded']:
                    continue
                quota = db.execute('SELECT value FROM meta WHERE key=?', ('searches:' + day,)).fetchone()
                used = int(quota['value']) if quota else 0
                if used >= 3:
                    break
                db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('searches:' + day, str(used + 1)))
                db.execute('UPDATE interests SET checked=? WHERE id=?', (now, item['id']))
            try:
                rows = self.search(item['topic'] + ' 공식 문서')
                def public(row):
                    if not isinstance(row, dict):
                        from dataclasses import asdict
                        row = asdict(row)
                    return {k: safe_text(row.get(k, ''), 500) for k in ('title', 'url', 'snippet', 'published_at')}
                results = [public(row) for row in rows[:4]]
                def official(row):
                    host = urlparse(row['url']).hostname or ''
                    return host.startswith(('docs.', 'developers.')) or host in {'github.com', 'learn.microsoft.com'}
                results.sort(key=lambda r: not official(r))
                if results:
                    self.add(item['task_id'], '작업 중 찾았던 “' + item['topic'][:80] + '” 관련 자료를 정리해 뒀어.',
                             {'source_task': item['task_id'], 'checked_at': now, 'results': results, 'content_read': False})
            except Exception:
                # Failure remains visible locally, without repeating a search or
                # claiming a source was checked successfully.
                self.store.event(task['session_id'], task['id'], 'RESEARCH_UNAVAILABLE', {'interest_id': item['id']})

    def import_legacy_events(self):
        """Read the old inbox without consuming, rewriting or executing it."""
        from pathlib import Path
        path = Path(__file__).resolve().parents[2]/'data/agency/inbox.jsonl'
        if not path.is_file() or path.is_symlink():
            return
        processed = set()
        state = path.parent/'state.json'
        if state.is_file() and not state.is_symlink() and state.stat().st_size < 128 * 1024:
            try:
                processed = set(json.loads(state.read_text()).get('processed', []))
            except (ValueError, TypeError, AttributeError):
                pass
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 512 * 1024))
            raw = stream.read(512 * 1024)
        for line in raw.decode('utf-8', errors='replace').splitlines()[-500:]:
            try:
                item = json.loads(line)
                if item['id'] in processed:
                    continue
                if item.get('ts') and self.store.clock() - datetime.fromisoformat(item['ts']).timestamp() > 7 * 86400:
                    continue
                self.ingest(item['kind'], item['text'], item['id'])
            except (ValueError, TypeError, KeyError):
                continue

    def interests(self, session, ident=None, action=None):
        if ident is not None:
            row = self.store.row('SELECT interests.* FROM interests JOIN tasks ON tasks.id=interests.task_id WHERE interests.id=? AND tasks.session_id=?', (ident, session))
            if not row or action not in {'keep', 'exclude'}:
                raise ValueError('INTEREST_NOT_FOUND')
            with self.store.transaction() as db:
                db.execute('UPDATE interests SET kept=?,excluded=? WHERE id=?', (int(action == 'keep'), int(action == 'exclude'), ident))
        rows = self.store.rows('SELECT interests.*,tasks.state AS task_state,tasks.updated AS task_updated FROM interests JOIN tasks ON tasks.id=interests.task_id WHERE tasks.session_id=? ORDER BY interests.created DESC LIMIT 50', (session,))
        return [{**r, 'expired': not r['kept'] and r['task_state'] in {'COMPLETED', 'CANCELLED'} and self.store.clock()-r['task_updated'] > 7 * 86400} for r in rows]

    def pending(self, session):
        rows = self.store.rows('SELECT suggestions.* FROM suggestions LEFT JOIN tasks ON tasks.id=suggestions.task_id WHERE delivered IS NULL AND (tasks.session_id=? OR suggestions.task_id LIKE ?) ORDER BY suggestions.created LIMIT 10', (session, 'event:%'))
        return [{**r, 'evidence': json.loads(r['evidence'])} for r in rows]

    def listing(self, session):
        rows = self.store.rows('SELECT suggestions.* FROM suggestions LEFT JOIN tasks ON tasks.id=suggestions.task_id WHERE tasks.session_id=? OR suggestions.task_id LIKE ? ORDER BY suggestions.created DESC LIMIT 20', (session, 'event:%'))
        return [{**r, 'evidence': json.loads(r['evidence'])} for r in rows]

    def claim_text(self, session, presence):
        # Same live-conversation, idle, spacing and daily quota rules. This does
        # not advertise audio readiness or certify speaker output.
        return self.claim_voice(session, {**presence, 'voice_ready': True} if presence else None)

    def claim_voice(self, session, presence):
        now = self.store.clock()
        if (not presence or not presence.get('voice_ready') or presence.get('busy')
                or now - presence.get('heartbeat', 0) > 5 or not 0 <= now - presence.get('last_user', 0) < 600):
            return None
        day = datetime.fromtimestamp(now, ZoneInfo('Asia/Seoul')).date().isoformat()
        with self.store.transaction() as db:
            last = db.execute("SELECT value FROM meta WHERE key='voice_last'").fetchone()
            used = db.execute('SELECT value FROM meta WHERE key=?', ('voice:' + day,)).fetchone()
            if (last and now - float(last['value']) < 1800) or (used and int(used['value']) >= 2):
                return None
            rows = self.pending(session)
            if not rows:
                return None
            item = rows[0]
            db.execute('UPDATE suggestions SET delivered=? WHERE id=? AND delivered IS NULL', (now, item['id']))
            db.execute("INSERT INTO meta VALUES('voice_last',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('voice:' + day, str(int(used['value']) + 1 if used else 1)))
            # Reserved delivery is not physical speaker confirmation. The client
            # separately records its actual audio outcome; uncertain speech does
            # not get repeated automatically.
            return item

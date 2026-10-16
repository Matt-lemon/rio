"""Fenced call ledger; transport attempts and observed outcomes are separate."""
from __future__ import annotations

import json
import re

from .types import ToolResult, encoded


PHASE_RANK = {'LEGACY_UNCLASSIFIED': -1, 'PREPARED': 0, 'ATTEMPTED': 1, 'ACCEPTED': 2, 'RESULT_CONFIRMED': 3}
UNRESOLVED = {'UNKNOWN', 'DISPATCHED', 'IN_FLIGHT', 'PREPARED'}


class Progress:
    def __init__(self, runner, claim, key):
        self.runner, self.claim, self.key = runner, claim, key

    def attempted(self, source='ADAPTER_BOUNDARY'):
        # This commit must succeed before crossing the dispatch boundary.
        self.runner.phase(self.key, 'ATTEMPTED', source, self.claim, running=True)

    def accepted(self, source='STRUCTURED_RECEIPT'):
        self.runner.phase(self.key, 'ACCEPTED', source, self.claim)


class ToolRunner:
    def __init__(self, store, catalog, adapters):
        self.store, self.catalog, self.adapters = store, catalog, adapters

    def phase(self, key, phase, source, claim=None, *, running=False, result=None):
        with self.store.transaction(claim, running=running, revision=running) as db:
            row = db.execute('SELECT * FROM calls WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('REQUEST_NOT_FOUND')
            if claim is not None and row['task_id'] != claim['id']:
                raise ValueError('REQUEST_ID_CONFLICT')
            state = result.status if result is not None else row['state']
            if result is None and phase == 'ATTEMPTED' and state == 'PREPARED':
                state = 'IN_FLIGHT'
            elif result is None and phase == 'ACCEPTED' and state in UNRESOLVED:
                state = 'DISPATCHED'
            current_phase = phase if PHASE_RANK[phase] >= PHASE_RANK[row['phase']] else row['phase']
            db.execute('INSERT INTO call_phases(call_id,phase,state,source,owner_token,fence,created) VALUES(?,?,?,?,?,?,?)',
                       (key, phase, state, source, self.store.executor_token or 0, claim['fence'] if claim else 0, self.store.clock()))
            db.execute('UPDATE calls SET phase=?,state=?,result=COALESCE(?,result),updated=? WHERE id=?',
                       (current_phase, state, encoded(result.public()) if result else None, self.store.clock(), key))

    def call(self, task, name, arguments, request_id, revision, browser):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise ValueError('INVALID_REQUEST_ID')
        tool = self.catalog.check(name, arguments, revision)
        effect = self.catalog.effect(tool, arguments)
        with self.store.transaction(task) as db:
            old = db.execute('SELECT * FROM calls WHERE id=?', (request_id,)).fetchone()
            if old:
                if old['task_id'] != task['id'] or old['name'] != name or old['args'] != encoded(arguments):
                    raise ValueError('REQUEST_ID_CONFLICT')
                return ToolResult.restore(json.loads(old['result'])) if old['result'] else ToolResult(
                    'UNKNOWN', '이전 요청 결과가 미확정이야. 다시 보내지 않을게.', request_id=request_id)
            now = self.store.clock()
            db.execute('INSERT INTO calls(id,task_id,name,args,revision,effect,state,result,created,updated,owner_token,fence,phase) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id, task['id'], name, encoded(arguments), revision, effect, 'PREPARED', None, now, now,
                        task['owner_token'], task['fence'], 'PREPARED'))
            db.execute('INSERT INTO call_phases(call_id,phase,state,source,owner_token,fence,created) VALUES(?,?,?,?,?,?,?)',
                       (request_id, 'PREPARED', 'PREPARED', 'DURABLE_RESERVATION', task['owner_token'], task['fence'], now))
        progress = Progress(self, task, request_id)
        try:
            # A takeover uses the same OS lock. It cannot overlap an old live
            # dispatch; death releases the lock. SQLite tokens fence late writes.
            with self.store.dispatch_lock():
                checked = self.catalog.check(name, arguments, revision)
                with self.store.transaction(task):
                    pass
                if hasattr(self.adapters, 'call_tracked'):
                    result = self.adapters.call_tracked(checked, arguments, request_id, browser, progress)
                else:
                    progress.attempted()
                    result = (self.adapters.call_checked(checked, arguments, request_id, browser)
                              if hasattr(self.adapters, 'call_checked') else self.adapters.call(name, arguments, request_id, browser))
                if not isinstance(result, ToolResult) or result.request_id not in {None, request_id}:
                    result = ToolResult('UNKNOWN', '도구 결과의 형식이나 요청 ID를 확인하지 못했어. 재전송하지 않을게.')
        except ValueError as error:
            if str(error) in {'EXECUTOR_FENCED', 'TASK_FENCED'}:
                raise
            code = str(error) if str(error) in {'TOOL_OR_ARGUMENTS_CHANGED', 'TOOL_REVISION_CHANGED'} else 'ADAPTER_ERROR'
            result = ToolResult('REJECTED' if code.startswith('TOOL_') else 'UNKNOWN',
                                '도구 설정이나 실행 결과를 확인하지 못했어. 자동으로 다시 보내지 않을게.', code=code)
        except Exception:
            result = ToolResult('UNKNOWN' if effect != 'read' else 'UNAVAILABLE',
                                '도구 연결이나 결과를 확인하지 못했어. 자동으로 다시 실행하지 않을게.', code='ADAPTER_UNAVAILABLE')
        result.request_id = request_id
        terminal = result.status not in UNRESOLVED
        if terminal:
            self.phase(request_id, 'RESULT_CONFIRMED', 'STRUCTURED_ADAPTER_RESULT', task, result=result)
        else:
            with self.store.transaction(task, running=False, revision=False) as db:
                db.execute('UPDATE calls SET state=?,result=?,updated=? WHERE id=?',
                           (result.status, encoded(result.public()), self.store.clock(), request_id))
        return result

    def status(self, request_id, session):
        row = self.store.row('SELECT calls.*,tasks.session_id FROM calls JOIN tasks ON tasks.id=calls.task_id WHERE calls.id=?', (request_id,))
        if row is None or row['session_id'] != session:
            raise ValueError('REQUEST_NOT_FOUND')
        if row['result']:
            result = ToolResult.restore(json.loads(row['result']))
            if result.status not in UNRESOLVED:
                return result
        try:
            result = self.adapters.status(row['name'], json.loads(row['args']), request_id)
            if not isinstance(result, ToolResult) or result.request_id not in {None, request_id}:
                raise ValueError('RESULT_ID_MISMATCH')
            if result.status not in {'SUCCEEDED', 'OBSERVED', 'SCHEDULED', 'FAILED', 'REJECTED', 'CANCELLED', 'UNKNOWN', 'DISPATCHED'}:
                raise ValueError('NO_EXISTING_RESULT')
        except Exception:
            return ToolResult('UNKNOWN', '이전 결과 조회에 실패했어. 요청을 다시 보내지 않을게.',
                              request_id=request_id, code='RESULT_LOOKUP_UNAVAILABLE')
        result.request_id = request_id
        if result.status not in UNRESOLVED:
            # Result lookup is allowed under the new owner, never a new dispatch.
            with self.store.transaction() as db:
                current = db.execute('SELECT state,result FROM calls WHERE id=?', (request_id,)).fetchone()
                if current['result'] and current['state'] not in UNRESOLVED:
                    return ToolResult.restore(json.loads(current['result']))
                db.execute('INSERT INTO events(task_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                           (row['task_id'], session, 'RESULT_RECONCILED', encoded({'request_id': request_id, 'result': result.public()}), self.store.clock()))
                for phase in ('ACCEPTED', 'RESULT_CONFIRMED'):
                    db.execute('INSERT INTO call_phases(call_id,phase,state,source,owner_token,fence,created) VALUES(?,?,?,?,?,?,?)',
                               (request_id, phase, result.status, 'EXISTING_ID_LOOKUP', self.store.executor_token or 0, 0, self.store.clock()))
                db.execute("UPDATE calls SET state=?,result=?,phase='RESULT_CONFIRMED',updated=? WHERE id=?",
                           (result.status, encoded(result.public()), self.store.clock(), request_id))
        return result

    def history(self, request_id, session):
        row = self.store.row('SELECT calls.id,calls.phase,calls.state,tasks.session_id FROM calls JOIN tasks ON tasks.id=calls.task_id WHERE calls.id=?', (request_id,))
        if row is None or row['session_id'] != session:
            raise ValueError('REQUEST_NOT_FOUND')
        return {'request_id': request_id, 'phase': row['phase'], 'state': row['state'],
                'transitions': self.store.rows('SELECT phase,state,source,owner_token,fence,created FROM call_phases WHERE call_id=? ORDER BY seq', (request_id,))}

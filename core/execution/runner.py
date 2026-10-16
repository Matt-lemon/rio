"""Durable reservation precedes every adapter entry; unknown writes never replay."""
from __future__ import annotations

import json
import re
import time

from .types import ToolResult, encoded


class ToolRunner:
    def __init__(self, store, catalog, adapters):
        self.store, self.catalog, self.adapters = store, catalog, adapters

    def call(self, task, name, arguments, request_id, revision, browser):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise ValueError('INVALID_REQUEST_ID')
        tool = self.catalog.check(name, arguments, revision)
        effect = self.catalog.effect(tool, arguments)
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM calls WHERE id=?', (request_id,)).fetchone()
            if old:
                if old['task_id'] != task['id'] or old['name'] != name or old['args'] != encoded(arguments):
                    raise ValueError('REQUEST_ID_CONFLICT')
                return ToolResult.restore(json.loads(old['result'])) if old['result'] else ToolResult(
                    'UNKNOWN', '이전 요청 결과가 미확정이야. 다시 보내지 않을게.', request_id=request_id)
            current = db.execute('SELECT state,revision FROM tasks WHERE id=?', (task['id'],)).fetchone()
            if not current or current['state'] != 'RUNNING' or current['revision'] != task['revision']:
                return ToolResult('REJECTED', '작업이 변경되어 다음 도구를 실행하지 않았어.', code='TASK_CHANGED')
            now = self.store.clock()
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (request_id, task['id'], name, encoded(arguments), revision, effect, 'IN_FLIGHT', None, now, now))
        try:
            # Recheck after durable reservation, before entering the adapter.
            checked = self.catalog.check(name, arguments, revision)
            current = self.store.task(task['id'])
            if current['revision'] != task['revision'] or current['state'] != 'RUNNING':
                result = ToolResult('REJECTED', '다음 도구 실행 전에 작업이 중지됐어.', code='TASK_CHANGED')
            else:
                if hasattr(self.adapters, 'call_checked'):
                    result = self.adapters.call_checked(checked, arguments, request_id, browser)
                else:
                    result = self.adapters.call(name, arguments, request_id, browser)
                if not isinstance(result, ToolResult):
                    result = ToolResult('UNKNOWN', '도구 결과 형식을 확인하지 못했어. 재전송하지 않을게.')
        except ValueError as error:
            code = str(error) if str(error) in {'TOOL_OR_ARGUMENTS_CHANGED', 'TOOL_REVISION_CHANGED'} else 'ADAPTER_ERROR'
            result = ToolResult('REJECTED' if code.startswith('TOOL_') else 'UNKNOWN',
                                '도구 설정이나 실행 결과를 확인하지 못했어. 자동으로 다시 보내지 않을게.', code=code)
        except Exception:
            result = ToolResult('UNKNOWN' if effect != 'read' else 'UNAVAILABLE',
                                '도구 연결이나 결과를 확인하지 못했어. 자동으로 다시 실행하지 않을게.', code='ADAPTER_UNAVAILABLE')
        result.request_id = result.request_id or request_id
        with self.store.transaction() as db:
            db.execute('UPDATE calls SET state=?,result=?,updated=? WHERE id=?',
                       (result.status, encoded(result.public()), self.store.clock(), request_id))
        return result

    def status(self, request_id, session):
        row = self.store.row('SELECT calls.*,tasks.session_id FROM calls JOIN tasks ON tasks.id=calls.task_id WHERE calls.id=?', (request_id,))
        if row is None or row['session_id'] != session:
            raise ValueError('REQUEST_NOT_FOUND')
        if row['result']:
            result = ToolResult.restore(json.loads(row['result']))
            if result.status not in {'UNKNOWN', 'DISPATCHED'}:
                return result
        try:
            result = self.adapters.status(row['name'], json.loads(row['args']), request_id)
        except Exception:
            return ToolResult('UNKNOWN', '이전 결과 조회에 실패했어. 요청을 다시 보내지 않을게.',
                              request_id=request_id, code='RESULT_LOOKUP_UNAVAILABLE')
        if result.status not in {'UNKNOWN', 'DISPATCHED'}:
            # Keep the original observation and its transition in the event log.
            self.store.event(session, row['task_id'], 'RESULT_RECONCILED', {'request_id': request_id, 'result': result.public()})
            with self.store.transaction() as db:
                db.execute('UPDATE calls SET state=?,result=?,updated=? WHERE id=?',
                           (result.status, encoded(result.public()), self.store.clock(), request_id))
        return result

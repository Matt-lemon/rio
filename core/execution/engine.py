"""Single-lane goal execution, independent of a connected terminal."""
from __future__ import annotations

import json
import re
import threading
import time

from core.context_tools import ContextTools, SENSITIVE
from core.research_browser import BrowserConversation
from .catalog import Catalog
from .adapters import Adapters
from .runner import ToolRunner
from .types import ToolResult, fingerprint, safe_text
from . import planner as models


class Task:
    def __init__(self, store, catalog=None, adapters=None, *, planner=models.next_step,
                 responder=models.reply, selector=None, monotonic=time.monotonic):
        self.store = store
        self.executor_token = store.activate_executor()
        self.catalog = catalog or Catalog()
        self.adapters = adapters or Adapters(store)
        self.runner = ToolRunner(store, self.catalog, self.adapters)
        self.planner, self.responder, self.selector = planner, responder, selector
        self.monotonic = monotonic
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.gate = threading.RLock()

    def submit(self, session, request):
        if not isinstance(request, str) or not 1 <= len(request) <= 2000 or SENSITIVE.search(request):
            raise ValueError('PUBLIC_REQUEST_REQUIRED')
        self.store.session(session)
        if len(self.store.rows("SELECT id FROM tasks WHERE state IN ('QUEUED','RUNNING') LIMIT 16")) >= 16:
            raise ValueError('TASK_QUEUE_FULL')
        self.store.append_turn(session, 'user', request)
        task = self.store.create_task(session, request)
        self.store.event(session, task['id'], 'TASK_QUEUED', {'id': task['id']})
        self.wake.set()
        return task

    def status(self, session, ident=None):
        if ident:
            return self.store.task(ident, session)
        return self.store.rows('SELECT * FROM tasks WHERE session_id=? ORDER BY created DESC LIMIT 20', (session,))

    def _target(self, session, ident):
        if ident:
            return self.store.task(ident, session)
        row = self.store.row("SELECT * FROM tasks WHERE session_id=? AND state NOT IN ('COMPLETED','FAILED','CANCELLED') ORDER BY created DESC LIMIT 1", (session,))
        if not row:
            raise ValueError('NO_ACTIVE_TASK')
        return row

    def pause(self, session, ident=None):
        return self._stop(session, ident, 'PAUSED')

    def cancel(self, session, ident=None):
        return self._stop(session, ident, 'CANCELLED')

    def _stop(self, session, ident, state):
        with self.gate, self.store.transaction() as db:
            task = self._target(session, ident)
            if task['state'] in {'COMPLETED', 'FAILED', 'CANCELLED'}:
                return task
            db.execute('UPDATE tasks SET state=?,revision=revision+1,updated=? WHERE id=?', (state, self.store.clock(), task['id']))
            db.execute('INSERT INTO events(task_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                       (task['id'], session, 'TASK_STOPPED', json.dumps({'state': state, 'message': '다음 단계는 멈췄어. 이미 전송된 요청의 취소나 원상복구를 뜻하지는 않아.'}, ensure_ascii=False), self.store.clock()))
            return self.store.task(task['id'])

    def resume(self, session, ident=None):
        with self.gate:
            task = self._target(session, ident)
            if task['state'] not in {'PAUSED', 'WAITING_INPUT', 'UNKNOWN'}:
                raise ValueError('TASK_NOT_RESUMABLE')
            for call in self.store.calls(task['id']):
                if self.runner.unresolved(call):
                    result = self.runner.status(call['id'], session)
                    if not result.completed:
                        state = 'UNKNOWN' if result.status in {'PREPARED', 'IN_FLIGHT', 'UNKNOWN', 'DISPATCHED'} else 'FAILED' if result.status == 'FAILED' else 'WAITING_INPUT'
                        self.store.update_task(task['id'], state=state, answer=result.message)
                        return self.store.task(task['id'])
                elif call['state'] not in {'SUCCEEDED', 'OBSERVED', 'RETURNED', 'SCHEDULED'}:
                    self.store.update_task(task['id'], answer='이전 단계는 실행 완료가 아니야. 같은 명령을 재전송하지 않고, 수정한 요청을 기다릴게.')
                    return self.store.task(task['id'])
            # Explicit user continuation replenishes only this execution slice.
            # Prior calls, their keys and evidence remain immutable history.
            self.store.update_task(task['id'], state='QUEUED', steps=0, elapsed=0)
            self.store.event(session, task['id'], 'USER_RESUMED', {})
            self.wake.set()
            return self.store.task(task['id'])

    def steer(self, session, ident, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 2000 or SENSITIVE.search(text):
            raise ValueError('PUBLIC_REQUEST_REQUIRED')
        with self.gate, self.store.transaction() as db:
            task = self.store.task(ident, session)
            if task['state'] not in {'RUNNING', 'QUEUED', 'PAUSED', 'WAITING_INPUT'}:
                raise ValueError('TASK_NOT_STEERABLE')
            db.execute('INSERT INTO events(task_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                       (ident, session, 'USER_CORRECTION', json.dumps({'request': text, 'request_epoch': task['revision'] + 1}, ensure_ascii=False), self.store.clock()))
            self.store.append_turn_db(db, session, 'user', text)
            db.execute("UPDATE tasks SET state='QUEUED',revision=revision+1,completion=?,steps=0,elapsed=0,direct=0,updated=? WHERE id=?",
                       (safe_text(text, 2000), self.store.clock(), ident))
        self.wake.set()
        return self.store.task(ident)

    def events(self, session, after=0):
        return self.store.events(session, after)

    def effective_request(self, task):
        row = self.store.row("SELECT payload FROM events WHERE task_id=? AND kind='USER_CORRECTION' ORDER BY seq DESC LIMIT 1", (task['id'],))
        return json.loads(row['payload'])['request'] if row else task['request']

    def request_epoch(self, task):
        row = self.store.row("SELECT payload FROM events WHERE task_id=? AND kind='USER_CORRECTION' ORDER BY seq DESC LIMIT 1", (task['id'],))
        return json.loads(row['payload']).get('request_epoch', 0) if row else 0

    def browser(self, session):
        browser = BrowserConversation()
        browser.session_id = session
        saved = self.store.session(session)['browser']
        age = self.store.clock() - saved.get('checked_at', 0)
        if 0 <= age < 600:
            browser.results = saved.get('results', [])
            browser.index = saved.get('index')
            browser.required_host = saved.get('required_host')
            browser.checked = time.monotonic() - age
        return browser

    def save_browser(self, session, browser, claim):
        self.store.browser(session, {'results': browser.results, 'index': browser.index,'required_host':browser.required_host,
                                    'checked_at': self.store.clock() - (time.monotonic() - browser.checked)}, claim=claim)

    def router(self, session):
        router = ContextTools(catalog_provider=lambda: {t['name']: t for t in self.catalog.list(planner=True)})
        if self.selector is not None:
            router.planner = self.selector
        # Only structured, successful outcomes ground implicit follow-up targets.
        rows = self.store.rows('SELECT calls.*,tasks.request FROM calls JOIN tasks ON tasks.id=calls.task_id WHERE tasks.session_id=? ORDER BY calls.created DESC LIMIT 6', (session,))
        cleared = self.store.row("SELECT created FROM events WHERE session_id=? AND kind='SESSION_CLEARED' ORDER BY seq DESC LIMIT 1", (session,))
        for row in reversed(rows):
            if cleared and row['created'] <= cleared['created']:
                continue
            if self.store.clock() - row['updated'] > 600 or not row['result']:
                continue
            result = ToolResult.restore(json.loads(row['result']))
            if result.status in {'SUCCEEDED', 'OBSERVED', 'SCHEDULED'}:
                router.remember(row['name'], row['request'], result.message, json.loads(row['args']))
        return router

    def finish(self, task, state, message):
        with self.gate:
            if not self.store.owns(task):
                return
            message = safe_text(message, 8000)
            with self.store.transaction(task) as db:
                db.execute('UPDATE tasks SET state=?,answer=?,updated=? WHERE id=?', (state, message, self.store.clock(), task['id']))
                self.store.append_turn_db(db, task['session_id'], 'assistant', message)
                db.execute('INSERT INTO events(task_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                           (task['id'], task['session_id'], 'TASK_FINISHED', json.dumps({'state': state, 'message': message}, ensure_ascii=False), self.store.clock()))

    @staticmethod
    def grounded_message(request, results, message):
        """Reject specific unsupported execution/content claims in final prose."""
        execution = r'실행했|켰어|껐어|열었어|재생했|등록했|제작했|완료했'
        if not results and re.search(r'켜|꺼|실행|열어|재생|정지|등록|제작', request) and re.search(execution, message):
            return '이번 요청에서 실제 도구를 실행한 기록은 없어. 필요한 동작을 확인한 뒤 진행할게.'
        if results and all(not r.get('evidence', {}).get('content_read') for r in results):
            if re.search(r'본문(?:을)?\s*(?:읽었|확인했)|페이지\s*내용(?:을)?\s*(?:읽었|확인했)', message):
                return '\n'.join(r['message'] for r in results[-2:]) + '\n페이지 본문은 읽지 않았어. 확인한 제목·주소·검색 요약문까지만 근거로 삼을 수 있어.'
        if any(r.get('confirmation') == 'SDK_ACK' for r in results) and not any(
                r.get('confirmation') in {'PHYSICAL_OBSERVATION', 'HUMAN_CONFIRMED'} for r in results):
            if re.search(r'실제.*(?:켜졌|꺼졌|작동.*확인|동작.*확인)', message):
                return '\n'.join(r['message'] for r in results[-2:]) + '\nSDK 응답이며 실제 기기 상태는 별도 확인이 필요해.'
        return message

    def run(self, ident):
        with self.gate:
            task = self.store.claim_task(ident)
            if task is None:
                return
        began = self.monotonic()
        session = task['session_id']
        request = self.effective_request(task)
        browser = self.browser(session)
        router = self.router(session)
        try:
            while not self.stopping.is_set():
                current = self.store.task(ident)
                if not self.store.owns(task):
                    return
                if current['steps'] >= 8 or current['elapsed'] + self.monotonic() - began >= 300:
                    self.finish(task, 'PAUSED', '여기까지 진행 내용을 저장했어. 한도에 도달했으니 /resume으로 이어갈 수 있어.')
                    return
                calls = self.store.calls(ident)
                unresolved = [c for c in calls if self.runner.unresolved(c)]
                if unresolved:
                    self.finish(task, 'UNKNOWN', '이전 요청 결과가 미확정이라 다음 단계를 멈췄어. /resume은 결과 조회만 먼저 하고 재전송하지 않아.')
                    return
                # Pausing changes the execution generation, not user authority.
                # Completed calls under the same request survive pause/resume.
                prefix = ident + '_' + str(self.request_epoch(task)) + '_'
                this_revision = [c for c in calls if c['id'].startswith(prefix)]
                history = self.store.session(session)['messages']
                if not this_revision:
                    decision = self.catalog.resolve(request, browser)
                    if decision is None:
                        decision = router.choose(request, history[:-1], browser)
                elif task['direct']:
                    decision = {'kind': 'done'}
                else:
                    payload = {'original_request': task['request'], 'current_user': request,
                               'completion_condition': task['completion'],
                               'tools': self.catalog.list(planner=True),
                               'results': [{'name': c['name'], 'arguments': c['args'], 'result': c['result']} for c in calls],
                               'browser_results': [{'index': r['index'], 'title': r['title']} for r in browser.results]}
                    decision = self.planner(payload)
                    if isinstance(decision, dict) and decision.get('kind') == 'tool':
                        feedback = None
                        try:
                            self.catalog.check(decision.get('name'), decision.get('arguments'))
                        except (ValueError, TypeError):
                            feedback = {'code': 'TOOL_OR_ARGUMENTS_CHANGED',
                                        'proposed_name': safe_text(decision.get('name', ''), 100),
                                        'argument_names': sorted(str(k)[:80] for k in decision.get('arguments', {}))
                                            if isinstance(decision.get('arguments'), dict) else [],
                                        'allowed_tools': [t['name'] for t in self.catalog.list(planner=True)]}
                            expected = self.catalog.describe(decision.get('name'))
                            if expected:
                                feedback['expected_arguments'] = list(expected['parameters'].get('properties', {}))
                        if feedback is None and any(c['name'] == decision.get('name') and c['args'] == decision.get('arguments') for c in this_revision):
                            feedback = {'code': 'STEP_ALREADY_RECORDED',
                                        'proposed_name': safe_text(decision.get('name', ''), 100),
                                        'instruction': 'Use the recorded result. Choose done if the goal is fulfilled, a different necessary step, or clarify. Never repeat the call.'}
                        if feedback is not None:
                            self.store.event(session, ident, 'PLAN_REJECTED', feedback, claim=task)
                            current = self.store.task(ident)
                            if not self.store.owns(task):
                                return
                            if current['elapsed'] + self.monotonic() - began >= 300:
                                self.finish(task, 'PAUSED', '계획 확인 중 한도에 도달해 저장했어. /resume으로 이어갈 수 있어.')
                                return
                            # One bounded model-only correction. No adapter call,
                            # new request ID or replay occurs during this repair.
                            decision = self.planner({**payload, 'validation_feedback': feedback})
                current = self.store.task(ident)
                if not self.store.owns(task):
                    return
                if not isinstance(decision, dict):
                    raise ValueError('INVALID_PLAN')
                kind = decision.get('kind')
                if kind in {'chat', 'done'}:
                    if kind == 'done' and not calls:
                        raise ValueError('NO_COMPLETION_EVIDENCE')
                    if kind == 'done' and any(not ToolResult.restore(c['result']).completed for c in this_revision if c['result']):
                        raise ValueError('INCOMPLETE_STEP')
                    results = [c['result'] for c in calls if c['result']]
                    # Single explicit tools return their own factual description.
                    # Only conversational/research synthesis uses the model.
                    if task['direct'] and this_revision:
                        message = this_revision[-1]['result']['message']
                    else:
                        message = self.responder(self.store.session(session), request, results)
                    if not isinstance(message, str) or not message.strip():
                        raise ValueError('EMPTY_REPLY')
                    message = self.grounded_message(request, results, message)
                    self.finish(task, 'COMPLETED', message)
                    return
                if kind == 'clarify':
                    question = decision.get('question')
                    if not isinstance(question, str) or not 1 <= len(question) <= 500 or SENSITIVE.search(question):
                        raise ValueError('INVALID_CLARIFICATION')
                    self.finish(task, 'WAITING_INPUT', question)
                    return
                if kind != 'tool' or not {'name', 'arguments'} <= set(decision):
                    raise ValueError('INVALID_PLAN')
                if current['elapsed'] + self.monotonic() - began >= 300:
                    self.finish(task, 'PAUSED', '다음 도구 실행 전에 시간 한도에 도달해 저장했어. /resume으로 이어갈 수 있어.')
                    return
                name, args = decision['name'], decision['arguments']
                tool = self.catalog.check(name, args)
                effect = self.catalog.effect(tool, args)
                if name == 'browser.open_url':
                    from core.browser_url import direct_url, public_url
                    requested = (request[len('/browse url '):].strip()
                                 if request.startswith('/browse url ') else direct_url(request))
                    if not public_url(args['url']) or requested != args['url']:
                        raise ValueError('UNREQUESTED_URL')
                if 'query' in args and (SENSITIVE.search(args['query']) or re.search(r'https?://|[\\/]|\b\d{6,}\b|[\n\r\t]', args['query'])):
                    raise ValueError('PRIVATE_QUERY_REJECTED')
                if name.startswith('legacy.') and (not decision.get('explicit') or args['command'] != request):
                    raise ValueError('EXPLICIT_COMMAND_REQUIRED')
                if name in {'pc.wake', 'pc.shutdown'} and not decision.get('explicit'):
                    raise ValueError('EXPLICIT_PC_COMMAND_REQUIRED')
                if effect == 'control' and not decision.get('explicit') and not router.control_allowed(name, args, request, history):
                    self.finish(task, 'WAITING_INPUT', '어느 기기에 어떤 동작을 할지 정확히 알려줘. 요청 범위를 넓혀 실행하지 않을게.')
                    return
                if name.startswith('spotify.timer.') and effect == 'control' and not decision.get('explicit'):
                    raise ValueError('EXPLICIT_TIMER_REQUIRED')
                if any(c['name'] == name and c['args'] == args for c in this_revision):
                    self.finish(task, 'WAITING_INPUT', '이미 처리한 단계를 반복하려 해서 멈췄어. 결과를 확인하고 다음 요청을 알려줘.')
                    return
                # A model cannot forge `explicit`: only local resolution creates it.
                trusted = self.catalog.resolve(request, browser) if decision.get('explicit') else None
                if decision.get('explicit') and trusted != decision:
                    raise ValueError('UNTRUSTED_EXPLICIT_FLAG')
                if not this_revision and decision.get('explicit'):
                    with self.store.transaction(task) as db:
                        db.execute('UPDATE tasks SET direct=1 WHERE id=?', (ident,))
                    task['direct'] = 1
                key = prefix + str(len(calls))
                self.store.event(session, ident, 'TOOL_START', {'name': name, 'request_id': key}, claim=task)
                result = self.runner.call(task, name, args, key, tool['revision'], browser)
                self.save_browser(session, browser, task)
                current = self.store.task(ident)
                if not self.store.owns(task):
                    return
                self.store.update_task(ident, claim=task, steps=current['steps'] + 1)
                self.store.event(session, ident, 'TOOL_RESULT', {'name': name, 'result': result.public()}, claim=task)
                if not result.completed:
                    state = 'UNKNOWN' if result.status in {'UNKNOWN', 'DISPATCHED'} else 'WAITING_INPUT' if result.status in {'NEED_INPUT', 'PREVIEW'} else 'FAILED'
                    self.finish(task, state, result.message)
                    return
        except Exception as error:
            if not self.store.owns(task):
                return
            code = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]{1,80}', str(error)) else 'EXECUTION_OR_MODEL_UNAVAILABLE'
            self.store.event(session, ident, 'EXECUTION_BLOCKED', {'error_type': type(error).__name__, 'code': code}, claim=task)
            self.finish(task, 'WAITING_INPUT', '다음 단계나 응답을 확정하지 못해 진행 내용을 저장했어. 도구를 자동으로 재전송하지 않을게.')
        finally:
            current = self.store.task(ident)
            # Never charge an old planner's elapsed time to a newer correction.
            if self.store.owns(task, running=False):
                try:
                    self.store.update_task(ident, claim=task, running=False, elapsed=current['elapsed'] + self.monotonic() - began)
                except ValueError as error:
                    if str(error) not in {'EXECUTOR_FENCED', 'TASK_FENCED'}:
                        raise

    def loop(self):
        while not self.stopping.is_set():
            row = self.store.row("SELECT id FROM tasks WHERE state='QUEUED' ORDER BY created LIMIT 1")
            if row:
                try:
                    self.run(row['id'])
                except ValueError as error:
                    if str(error) != 'EXECUTOR_FENCED':
                        raise
                    self.stopping.set()
                    return
            else:
                self.wake.wait(0.5)
                self.wake.clear()

"""Exercise the existing read-only Core and reopen its state; never call a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def open_task(root):
    from core.execution.catalog import Catalog
    from core.execution.engine import Task
    from core.execution.store import Store

    def no_model(*args, **kwargs):
        raise AssertionError('The offline demo must not call a model.')

    store = Store(root)
    task = Task(store, catalog=Catalog(provider=lambda: {}),
                selector=no_model, planner=no_model, responder=no_model)
    return store, task


def reopen(root, session, task_id, request_id):
    store, task = open_task(Path(root))
    try:
        row = task.status(session, task_id)
        calls = store.calls(task_id)
        assert row['state'] == 'COMPLETED'
        assert len(calls) == 1 and calls[0]['id'] == request_id
        print(json.dumps({'task_state': row['state'], 'same_request_id': True,
                          'request_id': request_id, 'new_dispatches': 0}))
    finally:
        store.close()


def main():
    if len(sys.argv) > 1:
        assert sys.argv[1] == '--reopen'
        return reopen(*sys.argv[2:])
    with tempfile.TemporaryDirectory(prefix='rio-offline-') as home:
        os.environ['HOME'] = home
        os.environ['USERPROFILE'] = home
        store, task = open_task(Path(home) / 'state')
        session = '0' * 32
        record = task.submit(session, '/core status')
        task.run(record['id'])
        calls = store.calls(record['id'])
        assert task.status(session, record['id'])['state'] == 'COMPLETED'
        assert len(calls) == 1 and calls[0]['name'] == 'core.status'
        request_id = calls[0]['id']
        print(json.dumps({'tool': calls[0]['name'], 'result': calls[0]['result'],
                          'phases': task.runner.history(request_id, session)['transitions']},
                         ensure_ascii=False), flush=True)
        store.close()
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--reopen',
                        str(Path(home) / 'state'), session, record['id'], request_id], check=True)


if __name__ == '__main__':
    main()

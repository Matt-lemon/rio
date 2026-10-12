import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture
def runtime(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root))
    if importlib.util.find_spec('yaml') is None:
        yaml = ModuleType('yaml')
        yaml.safe_load = lambda value: {}
        monkeypatch.setitem(sys.modules, 'yaml', yaml)
    # Any attempted voice import fails, rather than giving a usable fake voice.
    monkeypatch.setitem(sys.modules, 'rio_voice', None)
    monkeypatch.setitem(sys.modules, 'sounddevice', None)
    spec = importlib.util.spec_from_file_location('unified_test_runtime', root / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_shot_dubbing_does_not_start_chat_voice_or_memory(runtime, monkeypatch, capsys):
    handler = Mock(return_value='accepted')
    monkeypatch.setattr(runtime, 'handle_dubbing_command', handler)
    monkeypatch.setattr(runtime, '_run_chat', lambda *args: pytest.fail('chat started'))
    runtime.main(['--dubbing', 'status job_1'])
    handler.assert_called_once_with('/dubbing status job_1')
    assert capsys.readouterr().out.strip() == 'accepted'


def test_text_mode_never_imports_audio(runtime, monkeypatch):
    seen = []
    monkeypatch.setattr(runtime, '_run_chat', lambda voice: seen.append(voice))
    runtime.main(['--text-only'])
    assert len(seen) == 1 and isinstance(seen[0], runtime.TextVoice)


def test_home_stays_locked_and_dubbing_bypasses_memory_and_llm(runtime, monkeypatch, capsys):
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '1')
    import core.memory.nas_client as module
    memory = Mock()
    monkeypatch.setattr(module, 'NasMemoryClient', lambda: memory)
    monkeypatch.setattr(runtime, 'load_identity', lambda: {})
    monkeypatch.setattr(runtime, 'print_status', lambda *args: None)
    monkeypatch.setattr(runtime, 'handle_dubbing_command', lambda text: 'job queued' if text.startswith('/dubbing ') else None)
    monkeypatch.setattr(runtime, 'stream_completion', lambda *args: pytest.fail('LLM called'))
    inputs = iter(['/home turn on', '/dubbing auto pv.mp4', '/exit'])
    monkeypatch.setattr(runtime, 'read_user_input', lambda: next(inputs))
    runtime._run_chat(runtime.TextVoice())
    memory.safe_search.assert_not_called()
    assert '홈 제어는 잠겨' in capsys.readouterr().out


def test_memory_context_reaches_chat_and_evidence_gate(runtime, monkeypatch):
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '1')
    import core.memory.nas_client as module
    memory = Mock()
    memory.safe_search.return_value = [{'content': 'synthetic memory', 'score': .8}]
    monkeypatch.setattr(module, 'NasMemoryClient', lambda: memory)
    monkeypatch.setattr(runtime, 'load_identity', lambda: {})
    monkeypatch.setattr(runtime, 'print_status', lambda *args: None)
    monkeypatch.setattr(runtime, 'handle_dubbing_command', lambda text: None)
    gate = Mock(return_value={'action': 'allow'})
    monkeypatch.setattr(runtime, 'RIO_GATE_ENABLED', True)
    monkeypatch.setattr(runtime, 'call_rio_gate', gate)
    monkeypatch.setattr(runtime, 'choose_web_search', lambda *args: None)
    messages = []
    def stream(values):
        messages.extend(values)
        yield 'synthetic answer'
    monkeypatch.setattr(runtime, 'stream_completion', stream)
    inputs = iter(['내 사양 기억해?', '/exit'])
    monkeypatch.setattr(runtime, 'read_user_input', lambda: next(inputs))
    runtime._run_chat(runtime.TextVoice())
    gate.assert_called_once_with('내 사양 기억해?', has_memory=True)
    assert 'synthetic memory' in messages[0]['content']


def test_memory_shadow_preserves_pc_score_normalization(monkeypatch):
    from core.memory.nas_client import NasMemoryClient
    monkeypatch.setenv('RIO_MEMORY_SHADOW', '0')
    client = NasMemoryClient()
    client.shadow = Mock()
    rows = [{'content': 'first', '_score': .9}, {'content': 'bad', 'score': 'invalid'},
            'not a row', {'content': 'low', 'score': .1}]
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'results': rows}))))
    client.min_score = .6
    result = client.safe_search('synthetic')
    assert result == [{'content': 'first', '_score': .9, 'score': .9}]
    client.shadow.submit.assert_called_once()
    assert client.shadow.submit.call_args.args[1] == rows

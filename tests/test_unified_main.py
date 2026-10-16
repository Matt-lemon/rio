import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture
def runtime(monkeypatch):
    # A developer's selected production core must never redirect mocked tests
    # into a real browser, player or device service.
    monkeypatch.setenv('RIO_EXECUTION_CORE', '0')
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
    # Legacy route tests must not accidentally call the real model selector.
    monkeypatch.setattr(module, 'RIO_CONTEXT_TOOLS_ENABLED', False)
    return module


def test_explicit_core_flag_routes_to_service_presenter_only(runtime, monkeypatch):
    from core.execution import console
    presenter = Mock(return_value=True)
    monkeypatch.setattr(console, 'run', presenter)
    legacy = Mock(side_effect=AssertionError('legacy must not execute'))
    monkeypatch.setattr(runtime, 'handle_home_command', legacy)
    runtime.main(['--core', '--text-only', '--home', 'status'])
    assert presenter.call_args.kwargs['one_shot'] == '/home status'
    legacy.assert_not_called()


def test_core_submission_failure_never_falls_back_to_legacy(runtime, monkeypatch, capsys):
    from core.execution import console
    monkeypatch.setattr(console, 'run', Mock(side_effect=OSError('private endpoint unavailable')))
    legacy = Mock(side_effect=AssertionError('must not replay'))
    monkeypatch.setattr(runtime, 'handle_pc_command', legacy)
    runtime.main(['--core', '--text-only', '--pc', 'on'])
    assert '자동 재전송하지' in capsys.readouterr().out
    legacy.assert_not_called()


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


def test_browser_status_bypasses_chat_audio_and_model(runtime,monkeypatch,capsys):
    import core.research_browser as module
    handler=Mock(return_value='connected waiting')
    monkeypatch.setattr(module,'handle',handler)
    monkeypatch.setattr(runtime,'_run_chat',lambda *a:pytest.fail('chat started'))
    monkeypatch.setattr(runtime,'get_loaded_models',lambda *a:pytest.fail('model probed'))
    runtime.main(['--browse','status'])
    handler.assert_called_once_with('status')
    assert capsys.readouterr().out.strip()=='connected waiting'


def test_natural_search_result_selection_bypasses_other_actions(runtime, monkeypatch, capsys):
    import core.research_browser as browser
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '0')
    monkeypatch.setattr(runtime, 'print_status', lambda *a: None)
    handler = Mock(return_value='selected page opened')
    monkeypatch.setattr(browser.BrowserConversation, 'handle', lambda self, text: handler(text))
    for name in ['handle_home_command', 'handle_pc_command', 'handle_spotify_command',
                 'handle_dubbing_command', 'call_rio_gate', 'stream_completion']:
        monkeypatch.setattr(runtime, name, lambda *a, **kw: pytest.fail('other route called'))
    inputs = iter(['검색 결과 2번 열어줘', '/exit'])
    monkeypatch.setattr(runtime, 'read_user_input', lambda: next(inputs))
    runtime.main(['--text-only'])
    handler.assert_called_once_with('검색 결과 2번 열어줘')
    assert 'Rio: selected page opened' in capsys.readouterr().out


def test_one_shot_spotify_bypasses_chat_audio_and_models(runtime, monkeypatch, capsys):
    handler = Mock(side_effect=lambda text: 'music routed' if text == '/spotify play' else None)
    monkeypatch.setattr(runtime, 'handle_spotify_command', handler)
    monkeypatch.setattr(runtime, '_run_chat', lambda *a: pytest.fail('chat started'))
    monkeypatch.setattr(runtime, 'get_loaded_models', lambda: pytest.fail('model probed'))
    runtime.main(['--spotify', 'play'])
    assert capsys.readouterr().out.strip() == 'music routed'


def test_natural_music_uses_adapter_before_memory_gate_llm_and_home(runtime, monkeypatch, capsys):
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '1')
    import core.memory.nas_client as module
    memory = Mock()
    monkeypatch.setattr(module, 'NasMemoryClient', lambda: memory)
    monkeypatch.setattr(runtime, 'print_status', lambda *a: None)
    monkeypatch.setattr(runtime, 'handle_spotify_command', lambda text: 'music routed' if text == '음악 틀어줘' else None)
    for name in ['handle_home_command', 'handle_pc_command', 'handle_dubbing_command', 'call_rio_gate', 'stream_completion']:
        monkeypatch.setattr(runtime, name, lambda *a, **kw: pytest.fail('other route called'))
    inputs = iter(['음악 틀어줘', '/exit'])
    monkeypatch.setattr(runtime, 'read_user_input', lambda: next(inputs))
    runtime.main(['--text-only'])
    memory.safe_search.assert_not_called()
    assert 'Rio: music routed' in capsys.readouterr().out


def test_home_web_bypasses_chat_voice_and_model_loading(runtime, monkeypatch):
    import core.home.web_console as module
    handler = Mock()
    monkeypatch.setattr(module, 'run_home_web', handler)
    monkeypatch.setattr(runtime, '_run_chat', lambda *args: pytest.fail('chat started'))
    monkeypatch.setattr(runtime, 'get_loaded_models', lambda: pytest.fail('model probed'))
    runtime.main(['--home-web', '--home-web-port', '18445'])
    handler.assert_called_once_with(18445)


def test_one_shot_pc_bypasses_chat_and_audio(runtime, monkeypatch, capsys):
    handler = Mock(side_effect=lambda text: 'wake accepted' if text == '/pc on' else None)
    monkeypatch.setattr(runtime, 'handle_pc_command', handler)
    monkeypatch.setattr(runtime, '_run_chat', lambda *args: pytest.fail('chat started'))
    runtime.main(['--pc', 'on'])
    assert capsys.readouterr().out.strip() == 'wake accepted'


def test_natural_pc_request_does_not_probe_model_or_search_memory(runtime, monkeypatch, capsys):
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '1')
    import core.memory.nas_client as module
    memory = Mock()
    monkeypatch.setattr(module, 'NasMemoryClient', lambda: memory)
    monkeypatch.setattr(runtime, 'get_loaded_models', lambda: pytest.fail('PC model probed before wake'))
    monkeypatch.setattr(runtime, 'handle_pc_command', lambda text: 'wake accepted' if text == '컴퓨터 켜줘' else None)
    monkeypatch.setattr(runtime, 'call_rio_gate', lambda *args, **kwargs: pytest.fail('gate called'))
    monkeypatch.setattr(runtime, 'choose_web_search', lambda *args: pytest.fail('search called'))
    monkeypatch.setattr(runtime, 'stream_completion', lambda *args: pytest.fail('LLM called'))
    inputs = iter(['컴퓨터 켜줘', '/exit'])
    monkeypatch.setattr(runtime, 'read_user_input', lambda: next(inputs))
    runtime.main(['--text-only'])
    memory.safe_search.assert_not_called()
    assert 'Rio: wake accepted' in capsys.readouterr().out


def test_home_help_and_dubbing_bypass_memory_and_llm(runtime, monkeypatch, capsys):
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '1')
    import core.memory.nas_client as module
    memory = Mock()
    monkeypatch.setattr(module, 'NasMemoryClient', lambda: memory)
    monkeypatch.setattr(runtime, 'load_identity', lambda: {})
    monkeypatch.setattr(runtime, 'print_status', lambda *args: None)
    monkeypatch.setattr(runtime, 'handle_dubbing_command', lambda text: 'job queued' if text.startswith('/dubbing ') else None)
    monkeypatch.setattr(runtime, 'stream_completion', lambda *args: pytest.fail('LLM called'))
    inputs = iter(['/home help', '/dubbing auto pv.mp4', '/exit'])
    monkeypatch.setattr(runtime, 'read_user_input', lambda: next(inputs))
    runtime._run_chat(runtime.TextVoice())
    memory.safe_search.assert_not_called()
    assert '조명 명령:' in capsys.readouterr().out


@pytest.mark.parametrize('action', ['off', 'status'])
def test_pc_shutdown_and_status_bypass_chat(runtime, monkeypatch, capsys, action):
    monkeypatch.setattr(runtime, 'handle_pc_command', lambda text: 'PC routed' if text == '/pc '+action else None)
    monkeypatch.setattr(runtime, '_run_chat', lambda *args: pytest.fail('chat started'))
    runtime.main(['--pc', action])
    assert capsys.readouterr().out.strip() == 'PC routed'


def test_one_shot_google_home_bypasses_chat_and_audio(runtime, monkeypatch, capsys):
    monkeypatch.setattr(runtime, 'handle_home_command', lambda text: 'light routed' if text == '/home 거실 on' else None)
    monkeypatch.setattr(runtime, '_run_chat', lambda *args: pytest.fail('chat started'))
    runtime.main(['--home', '거실 on'])
    assert capsys.readouterr().out.strip() == 'light routed'


def test_natural_light_does_not_search_memory_gate_or_llm(runtime, monkeypatch, capsys):
    monkeypatch.setenv('RIO_MEMORY_ENABLED', '1')
    import core.memory.nas_client as module
    memory=Mock();monkeypatch.setattr(module, 'NasMemoryClient', lambda: memory)
    monkeypatch.setattr(runtime, 'get_loaded_models', lambda: pytest.fail('model probed'))
    monkeypatch.setattr(runtime, 'call_rio_gate', lambda *a, **kw: pytest.fail('gate called'))
    monkeypatch.setattr(runtime, 'stream_completion', lambda *a: pytest.fail('LLM called'))
    monkeypatch.setattr(runtime, 'handle_home_command', lambda text: 'light routed' if text=='거실 불 켜줘' else None)
    inputs=iter(['거실 불 켜줘','/exit']);monkeypatch.setattr(runtime,'read_user_input',lambda:next(inputs))
    runtime.main(['--text-only'])
    memory.safe_search.assert_not_called()
    assert 'Rio: light routed' in capsys.readouterr().out


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

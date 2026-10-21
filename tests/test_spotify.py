import json
import subprocess
from unittest.mock import Mock

import pytest

from core.spotify import LocalSpotify, SpotifyController, handle_spotify_command, parse_spotify_request


@pytest.mark.parametrize('text', [
    '음악 틀어줘', '노래 좀 틀어줘', '노래를 틀어주세요', '리오야 음악 재생해줘',
    '재생해줘', '다시 재생해줘', '계속 틀어줘', '이어서 재생해줘', '다시틀어줘',
    '스포티파이 재생해줘', 'Spotify play', '/spotify play', '노래 켜줘',
    '음악 재생 부탁해', '음악 틀어줄래?', '재생해 줄래요?', '노래틀어봐',
])
def test_play_variants(text):
    if text == 'Spotify play':
        assert parse_spotify_request(text) is None
    else:
        assert parse_spotify_request(text) == 'play'


@pytest.mark.parametrize('text', [
    '음악 일시정지해줘', '노래 좀 멈춰줘', '일시정지해줘', '잠깐 멈춰줘',
    '잠시 멈춰주세요', '스포티파이 일시 정지 해 줘', '음악 꺼줘',
    '리오야, 노래를 멈춰줄래?', '/spotify pause', '일시정지 부탁해',
])
def test_pause_variants(text):
    assert parse_spotify_request(text) == 'pause'


@pytest.mark.parametrize('text', [
    '꺼줘', '켜줘', '에어컨 꺼줘', '컴퓨터 종료해줘', '사운드바 재생해줘',
    '노래 틀지마', '음악 재생하지 마', '음악 틀 수 있어?', '음악 재생할까?',
    '음악 재생 중이야?', '"음악 틀어줘"', '음악 틀어줘라고 말했어',
    '음악 틀어주고 컴퓨터 꺼줘', '내일 음악 틀어줘', '/spotify next',
    'if needed 음악 틀어줘', '음악 틀어줘; rm -rf /',
])
def test_ambiguous_noncommands_and_other_devices_do_not_act(text):
    assert parse_spotify_request(text) is None
    factory = Mock()
    handle_spotify_command(text, factory)
    factory.assert_not_called()


def snapshot(state='Paused', owner=':1.140', **changes):
    value = {'owner': owner, 'playback': state, 'can_control': True, 'can_play': True, 'can_pause': True}
    return dict(value, **changes)


def test_play_uses_one_explicit_command_and_verifies_same_player():
    transport = Mock()
    transport.snapshot.side_effect = [snapshot(), snapshot('Paused'), snapshot('Playing')]
    controller = SpotifyController(transport, sleep=Mock())
    assert controller.call('play') == {'status': 'SUCCEEDED', 'playback': 'Playing', 'command_sent': True}
    transport.command.assert_called_once_with(':1.140', 'play')


def test_pause_uses_pause_not_toggle():
    transport = Mock()
    transport.snapshot.side_effect = [snapshot('Playing'), snapshot('Paused')]
    assert SpotifyController(transport).call('pause')['status'] == 'SUCCEEDED'
    transport.command.assert_called_once_with(':1.140', 'pause')


def test_already_at_target_does_not_send_or_toggle():
    transport = Mock()
    transport.snapshot.return_value = snapshot('Playing')
    assert SpotifyController(transport).call('play')['command_sent'] is False
    transport.command.assert_not_called()


@pytest.mark.parametrize('key', ['can_control', 'can_play'])
def test_player_permission_checked_before_call(key):
    transport = Mock()
    transport.snapshot.return_value = snapshot(**{key: False})
    assert SpotifyController(transport).call('play')['status'] == 'REJECTED'
    transport.command.assert_not_called()


def test_uncertain_delivery_is_never_retried_and_raw_error_is_hidden():
    transport = Mock()
    transport.snapshot.return_value = snapshot()
    transport.command.side_effect = subprocess.TimeoutExpired('private-value', 4)
    result = SpotifyController(transport).call('play')
    assert result['status'] == 'UNKNOWN'
    assert 'private-value' not in str(result)
    assert transport.command.call_count == 1
    assert transport.snapshot.call_count == 1


def test_player_restart_cannot_confirm_or_retarget_write():
    transport = Mock()
    transport.snapshot.side_effect = [snapshot(), snapshot('Playing', owner=':1.141')]
    assert SpotifyController(transport).call('play')['status'] == 'UNKNOWN'
    transport.command.assert_called_once_with(':1.140', 'play')


def test_successful_method_without_state_change_is_unconfirmed():
    transport = Mock()
    transport.snapshot.return_value = snapshot()
    assert SpotifyController(transport, sleep=Mock()).call('play')['status'] == 'UNKNOWN'
    assert transport.command.call_count == 1


@pytest.mark.parametrize('action', ['toggle', 'next', 'volume', 'open', 'play;shutdown'])
def test_only_small_allowlist_available(action):
    transport = Mock()
    assert SpotifyController(transport).call(action)['status'] == 'REJECTED'
    transport.snapshot.assert_not_called()
    with pytest.raises(ValueError):
        LocalSpotify().command(':1.140', action)


def test_status_returns_only_approved_state_no_owner_or_metadata():
    transport = Mock()
    transport.snapshot.return_value = dict(snapshot(), metadata='private-song')
    assert SpotifyController(transport).call('status') == {'status': 'OBSERVED', 'playback': 'Paused'}
    transport.command.assert_not_called()


def test_bus_calls_are_fixed_argv_no_shell_and_no_arbitrary_environment(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
    monkeypatch.setattr(subprocess, 'run', run)
    LocalSpotify().command(':1.140', 'pause')
    args, kwargs = run.call_args
    assert args[0] == ['/usr/bin/busctl', '--user', '--json=short', '--timeout=3',
                       'call', ':1.140', '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player', 'Pause']
    assert kwargs.get('shell', False) is False
    assert 'LD_PRELOAD' not in kwargs['env']


def test_invalid_property_types_are_rejected():
    transport = LocalSpotify()
    transport._run = Mock(return_value=json.dumps({'type': 'b', 'data': 'true'}))
    with pytest.raises(ValueError):
        transport._properties(':1.140', 'interface', ('CanPlay',), (bool,))


def test_wrong_player_identity_cannot_be_controlled():
    transport = LocalSpotify()
    transport._run = Mock(side_effect=[json.dumps({'type': 's', 'data': [':1.140']}),
                                      '\n'.join(json.dumps({'type': 's', 'data': v}) for v in ['Browser', 'browser'])])
    assert SpotifyController(transport).call('play')['status'] == 'UNAVAILABLE'
    assert transport._run.call_count == 2

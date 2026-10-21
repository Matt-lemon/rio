from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.spotify import SpotifyController, handle_spotify_command
from core.spotify_timer import (KST, TimerParseError, TimerStore, duration_seconds,
                                format_deadline, handle_timer_command, parse_timer_request)

NOW = datetime(2026, 10, 15, 10, 45, 0, tzinfo=KST)


@pytest.mark.parametrize('text,minutes,play', [
    ('30분 뒤 음악 멈춰줘', 30, False), ('노래 10분 후에 꺼줘', 10, False),
    ('음악 1시간만 틀어줘', 60, True), ('1시간30분 동안 재생해줘', 90, True),
    ('리오야 한시간만 틀어줘', 60, True), ('두 시간 뒤 멈춰줘', 120, False),
    ('반시간만 틀어줘', 30, True), ('한시간 반 후 일시정지해줘', 90, False),
    ('삼십분 뒤 멈춰줘', 30, False), ('/spotify timer after 30m', 30, False),
])
def test_relative_times_use_the_current_wall_clock(text, minutes, play):
    result = parse_timer_request(text, NOW)
    assert result == {'kind': 'schedule', 'due': NOW.timestamp()+minutes*60, 'play': play}


@pytest.mark.parametrize('text,day,hour,minute,play', [
    ('오늘 18:30에 음악 멈춰줘', 15, 18, 30, False),
    ('23:00에 멈춰줘', 15, 23, 0, False),
    ('오늘 밤 11시까지 음악 틀어줘', 15, 23, 0, True),
    ('내일 00:30까지', 16, 0, 30, False),
    ('내일 오전 7시에 노래 꺼줘', 16, 7, 0, False),
    ('오늘 오후 6시 반에 일시정지해줘', 15, 18, 30, False),
    ('18시 30분까지만 틀어줘', 15, 18, 30, True),
    ('내일 0시에 멈춰줘', 16, 0, 0, False),
    ('오늘 밤 12시에 음악 꺼줘', 16, 0, 0, False),
    ('오후 12시까지', 15, 12, 0, False),
    ('모레 21:05까지', 17, 21, 5, False),
])
def test_korean_periods_dates_and_24_hour_time(text, day, hour, minute, play):
    result = parse_timer_request(text, NOW)
    expected = datetime(2026, 10, day, hour, minute, tzinfo=KST).timestamp()
    assert result == {'kind': 'schedule', 'due': expected, 'play': play}


@pytest.mark.parametrize('text,code', [
    ('6시에 멈춰줘', 'AMBIGUOUS_CLOCK'), ('내일 7시까지', 'AMBIGUOUS_CLOCK'),
    ('오늘 09:00에 멈춰줘', 'PAST_CLOCK'), ('00:30까지', 'PAST_CLOCK'),
    ('24:00에 멈춰줘', 'BAD_CLOCK'), ('18:75에 멈춰줘', 'BAD_CLOCK'),
    ('오후 18시에 꺼줘', 'BAD_CLOCK'), ('0분 뒤 멈춰줘', 'BAD_DURATION'),
])
def test_past_ambiguous_and_invalid_times_are_not_silently_guessed(text, code):
    with pytest.raises(TimerParseError, match=code):
        parse_timer_request(text, NOW)


@pytest.mark.parametrize('text', [
    '에어컨 30분 후 꺼줘', '컴퓨터 1시간 뒤 멈춰줘', '타이머 만들 수 있어?',
    '음악 30분 뒤 멈추지마', '"30분 뒤 멈춰줘"', '30분 뒤 멈춰줘라고 말했어',
    '내일 비 오면 음악 30분만 틀어줘', '30분 뒤 멈춰주고 컴퓨터 꺼줘', '얼마나 남았어?',
])
def test_unrelated_devices_negations_quotes_and_conditions_cannot_arm(text):
    factory = Mock()
    # Other-device requests may produce a time-format explanation, but never a timer or device write.
    reply = handle_timer_command(text, store_factory=factory, now=NOW)
    factory.assert_not_called()


@pytest.mark.parametrize('text,kind', [
    ('음악 타이머 취소해줘', 'cancel'), ('타이머 상태', 'status'),
    ('노래 타이머 얼마나 남았어?', 'status'), ('/spotify timer cancel', 'cancel'),
    ('/spotify timer status', 'status'),
])
def test_status_and_cancel_variants(text, kind):
    assert parse_timer_request(text, NOW) == {'kind': kind}


def make_store(tmp_path, clock=None, active=None):
    return TimerStore(tmp_path/'private', clock=clock or Mock(return_value=NOW.timestamp()),
                      active=active or Mock(return_value=True))


def test_schedule_survives_new_instance_and_fires_only_once(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    first = make_store(tmp_path, clock)
    assert first.schedule(clock()+30)['status'] == 'SCHEDULED'
    resumed = make_store(tmp_path, clock)
    assert resumed.status()['state'] == 'ARMED'
    controller = Mock()
    controller.call.return_value = {'status': 'SUCCEEDED', 'playback': 'Paused'}
    assert resumed.run_due(controller) is None
    clock.return_value += 30
    assert resumed.run_due(controller)['state'] == 'FINISHED'
    assert resumed.run_due(controller) is None
    controller.call.assert_called_once_with('pause')
    assert resumed.status()['state'] == 'FINISHED'
    assert (tmp_path/'private').stat().st_mode & 0o077 == 0
    assert (tmp_path/'private/timers.sqlite3').stat().st_mode & 0o077 == 0


def test_cancel_and_replacement_never_fire_old_requests(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    store = make_store(tmp_path, clock)
    first = store.schedule(clock()+10)
    replacement = store.schedule(clock()+30)
    assert replacement['replaced'] is True
    clock.return_value += 10
    controller = Mock()
    assert store.run_due(controller) is None
    assert store.cancel()['state'] == 'CANCELLED'
    clock.return_value += 30
    assert store.run_due(controller) is None
    controller.call.assert_not_called()
    with store.database() as db:
        assert db.execute('SELECT state FROM timers WHERE id=?', (first['id'],)).fetchone()[0] == 'REPLACED'


def test_worker_unavailable_never_promises_a_timer(tmp_path):
    store = make_store(tmp_path, active=Mock(return_value=False))
    assert store.schedule(NOW.timestamp()+30)['code'] == 'TIMER_WORKER_UNAVAILABLE'
    assert not (tmp_path/'private').exists()


def test_late_wakeup_is_missed_and_not_sent(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    store = make_store(tmp_path, clock)
    store.schedule(clock()+30)
    clock.return_value += 100
    controller = Mock()
    assert store.run_due(controller)['state'] == 'MISSED'
    controller.call.assert_not_called()


def test_uncertain_pause_is_never_replayed_after_recovery(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    store = make_store(tmp_path, clock)
    store.schedule(clock()+1)
    clock.return_value += 1
    controller = Mock()
    controller.call.return_value = {'status': 'UNKNOWN', 'code': 'DELIVERY_UNCERTAIN'}
    assert store.run_due(controller)['state'] == 'UNKNOWN'
    store.recover()
    assert store.run_due(controller) is None
    assert controller.call.call_count == 1


def test_crash_after_claim_is_unknown_and_not_replayed(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    store = make_store(tmp_path, clock)
    store.schedule(clock()+1)
    clock.return_value += 1
    controller = Mock()
    controller.call.side_effect = RuntimeError('simulated process crash')
    with pytest.raises(RuntimeError):
        store.run_due(controller)
    assert store.status()['state'] == 'EXECUTING'
    assert store.cancel()['state'] == 'EXECUTING'
    assert store.schedule(clock()+30)['code'] == 'TIMER_ALREADY_EXECUTING'
    store.recover()
    assert store.status()['state'] == 'UNKNOWN'
    assert store.run_due(controller) is None
    assert controller.call.call_count == 1


def test_concurrent_workers_claim_only_once(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    store = make_store(tmp_path, clock)
    store.schedule(clock()+1)
    clock.return_value += 1
    controller = Mock()
    controller.call.return_value = {'status': 'SUCCEEDED', 'playback': 'Paused'}
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: make_store(tmp_path, clock).run_due(controller), range(2)))
    controller.call.assert_called_once_with('pause')


def test_time_moves_backwards_without_firing_early(tmp_path):
    clock = Mock(return_value=NOW.timestamp())
    store = make_store(tmp_path, clock)
    store.schedule(clock()+30)
    clock.return_value -= 300
    controller = Mock()
    assert store.run_due(controller) is None
    controller.call.assert_not_called()


def test_private_directory_and_db_link_guards(tmp_path):
    target = tmp_path/'public'
    target.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        with TimerStore(target).database():
            pass
    private = tmp_path/'private'
    private.mkdir(mode=0o700)
    destination = tmp_path/'original'
    destination.write_text('keep')
    (private/'timers.sqlite3').symlink_to(destination)
    with pytest.raises(OSError):
        with TimerStore(private).database():
            pass
    assert destination.read_text() == 'keep'


def test_combined_play_is_guarded_by_successful_timer_registration(tmp_path):
    transport = Mock()
    transport.snapshot.return_value = {'owner': ':1.1', 'playback': 'Paused', 'can_control': True, 'can_play': True}
    controller = SpotifyController(transport)
    store = make_store(tmp_path, active=Mock(return_value=False))
    reply = handle_timer_command('음악 30분만 틀어줘', store_factory=lambda: store,
                                 controller_factory=lambda: controller, now=NOW)
    assert '등록하지 못했어요' in reply
    transport.command.assert_not_called()


def test_combined_play_retains_stop_timer_if_delivery_unknown(tmp_path):
    transport = Mock()
    transport.snapshot.return_value = {'owner': ':1.1', 'playback': 'Paused', 'can_control': True, 'can_play': True}
    transport.command.side_effect = OSError('uncertain')
    store = make_store(tmp_path)
    reply = handle_timer_command('음악 30분만 틀어줘', store_factory=lambda: store,
                                 controller_factory=lambda: SpotifyController(transport), now=NOW)
    assert '정지 예약은 유지해요' in reply
    assert store.status()['state'] == 'ARMED'
    assert transport.command.call_count == 1


def test_ambiguous_time_does_not_create_store_or_start_music():
    store, controller = Mock(), Mock()
    assert '오전·오후' in handle_timer_command('6시까지 틀어줘', store, controller, now=NOW)
    store.assert_not_called()
    controller.assert_not_called()


def test_schedule_deadline_revalidated_after_database_wait(tmp_path):
    clock = Mock(side_effect=[NOW.timestamp(), NOW.timestamp()+15])
    store = make_store(tmp_path, clock)
    assert store.schedule(NOW.timestamp()+20, minimum_delay=10)['code'] == 'INVALID_DEADLINE'
    assert store.status()['state'] == 'NONE'

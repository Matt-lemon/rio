"""Exercise real voice queues and processing without playing sound or calling TTS."""
import importlib.util
from pathlib import Path
import queue
import subprocess
import threading
from unittest.mock import Mock

import pytest

import rio_voice as voice


def instance():
    item=voice.RioVoice.__new__(voice.RioVoice)
    item._audio_queue=queue.Queue();item.audio_succeeded=False;item.audio_failed=False
    return item


def test_pipewire_preserves_processed_pcm_and_waits_for_player_exit(monkeypatch):
    item=instance();item._audio_queue.put(b'\x01\x00'*240)
    entered=threading.Event();release=threading.Event()
    def play(*args,**kwargs):
        entered.set();assert release.wait(3)
        assert kwargs['check'] is True and kwargs['timeout']>20
        assert kwargs['input']==b'\0\0'*3600+b'processed'
        assert args[0][:3]==['/usr/bin/pw-play','--raw','--rate']
        assert '--volume' in args[0]
    monkeypatch.setattr(voice.subprocess,'run',play)
    eq=Mock();eq.process_pcm16.return_value=b'equalized'
    deesser=Mock();deesser.process_pcm16.return_value=b'processed'
    worker=threading.Thread(target=item._pipewire_audio_worker,args=(eq,deesser),daemon=True);worker.start()
    try:
        assert entered.wait(3)
        assert not item.audio_succeeded and item._audio_queue.unfinished_tasks==1
    finally:
        release.set();item._audio_queue.put(None);worker.join(3)
    assert not worker.is_alive() and item._audio_queue.unfinished_tasks==0
    eq.process_pcm16.assert_called_once_with(b'\x01\x00'*240)
    deesser.process_pcm16.assert_called_once_with(b'equalized')
    assert item.audio_succeeded


@pytest.mark.parametrize('failure',[subprocess.CalledProcessError(1,['pw-play']),subprocess.TimeoutExpired(['pw-play'],20)])
def test_playback_failure_drains_queues_and_cannot_confirm_audio(monkeypatch,failure):
    item=instance();item._audio_queue.put(b'\1\0'*240);item._audio_queue.put(None)
    monkeypatch.setattr(voice,'sd',None)
    monkeypatch.setattr(voice.sys,'platform','linux')
    monkeypatch.setattr(voice.subprocess,'run',Mock(side_effect=failure))
    item._audio_worker()
    assert item.audio_failed and not item.audio_succeeded
    assert item._audio_queue.unfinished_tasks==0


def test_missing_portaudio_import_still_allows_nas_voice_processing(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'sounddevice',None)
    spec=importlib.util.spec_from_file_location('voice_without_portaudio',Path(voice.__file__))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.sd is None
    assert module.SAMPLE_RATE==24000 and module.RIO_EQ_BANDS==voice.RIO_EQ_BANDS
    assert module.RIO_DEESSER_THRESHOLD_DB==voice.RIO_DEESSER_THRESHOLD_DB


@pytest.mark.parametrize('body,content_type',[
    (b'RIFF'*4,'application/octet-stream'),(b'{}','application/json'),
    (b'bad','audio/pcm'),(b'','audio/pcm'),(b'ID3tag','application/octet-stream'),
])
def test_incompatible_nas_tts_responses_never_reach_speaker_queue(monkeypatch,body,content_type):
    item=instance();item._tts_queue=queue.Queue();item._tts_queue.put('안녕하세요.');item._tts_queue.put(None)
    response=Mock(content=body,headers={'Content-Type':content_type})
    session=Mock();session.post.return_value=response
    context=Mock();context.__enter__=Mock(return_value=session);context.__exit__=Mock(return_value=False)
    monkeypatch.setattr(voice.requests,'Session',lambda:context)
    item._tts_worker()
    assert item.audio_failed and not item.audio_succeeded
    assert item._audio_queue.empty() and item._tts_queue.unfinished_tasks==0
    session.post.assert_called_once()


def test_nas_tts_contract_retains_voice_and_pcm_settings(monkeypatch):
    item=instance();item._tts_queue=queue.Queue();item._tts_queue.put('안녕하세요.');item._tts_queue.put(None)
    pcm=b'\1\0'*240
    response=Mock(content=pcm,headers={'Content-Type':'audio/pcm; rate=24000'})
    session=Mock();session.post.return_value=response
    context=Mock();context.__enter__=Mock(return_value=session);context.__exit__=Mock(return_value=False)
    monkeypatch.setattr(voice.requests,'Session',lambda:context)
    item._tts_worker()
    assert item._audio_queue.get_nowait()==pcm and not item.audio_succeeded
    call=session.post.call_args
    assert call.args==('http://127.0.0.1:8091/v1/audio/speech',)
    assert call.kwargs['json']=={'input':'안녕하세요.','voice':'rio8','language':'Korean','response_format':'pcm','seed':42,'instructions':'korean accent'}

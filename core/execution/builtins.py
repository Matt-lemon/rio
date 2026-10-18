"""Trusted built-in declarations; read binding metadata afresh on every discovery."""
import json
import os
from pathlib import Path
from .types import spec, enum, TEXT

def builtin_contracts():
    """Read local binding metadata only. No credentials, API calls or ledger writes."""
    tools=[
        spec('browser.search','전용 Edge에 검색 결과를 보여준다.', 'navigate',query=TEXT),
        spec('browser.open_url','사용자가 준 HTTPS 공개 URL을 리오 전용 브라우저에서 직접 연다.', 'navigate',
             url={'type':'string','minLength':1,'maxLength':2048}),
        spec('browser.open_topic','주제에 맞는 공개 페이지를 검색하고 연다. 뉴스 기사 열기 포함.', 'navigate',query=TEXT),
        spec('browser.open_result','이 대화에서 확인한 검색 결과 번호를 연다.', 'navigate',index={'type':'integer','minimum':1,'maximum':5}),
        spec('browser.back_results','이 대화의 마지막 검색 결과 목록으로 돌아간다.', 'navigate'),
        spec('browser.status','전용 브라우저 연결과 현재 허용된 페이지 상태.', 'read'),
        spec('web.search','화면을 열 필요 없이 최신 사실·용어·출처를 찾아 답한다.', 'read',query=TEXT),
        spec('spotify.control','NAS Spotify 음악 상태·재생·일시정지. TV나 브라우저 영상 조작 아님.', 'control',action=enum('status','play','pause')),
        spec('home.status','Google Home 연결 상태를 읽는다.', 'read'),
        spec('home.smartthings', '공식 SmartThings CLI에서 고정된 조명의 상태 조회 또는 명시적 1회 전원 제어.', 'control',
             target=enum('주방', '거실'), action=enum('status', 'on', 'off')),
        spec('pc.status','메인 PC 상태를 읽는다. 전원 조작은 제공하지 않는다.', 'read'),
        spec('dubbing.health','기존 더빙 서버 상태만 읽는다. 서버 시작·영상 제출 아님.', 'read'),
    ]
    from core.home.smartthings_pc import read_private,PcError
    try:
        from core.home.tools import validate_targets
        p=Path(os.getenv('RIO_HOME_TARGET_FILE',str(Path.home()/'.config/rio/google-home-lights.json')))
        lights=validate_targets(json.loads(read_private(p))) if p.exists() else {}
        flag=os.getenv('RIO_HOME_EXECUTION_ENABLED')
        if flag is not None:
            if flag.lower() not in {'1','true','yes'}:lights={}
        else:
            p=Path(os.getenv('RIO_HOME_POLICY_FILE',str(Path.home()/'.config/rio/google-home-policy.json')))
            policy=json.loads(read_private(p)) if p.exists() else {}
            lights={k:v for k,v in lights.items() if policy.get('enabled') is True and v['alias'] in policy.get('allowed_aliases',[])}
        if lights:tools.append(spec('home.light','등록 조명 켜기·끄기만. 밝기·온도·PC·환풍기·청소기 제외.', 'control',target=enum(*lights),action=enum('on','off')))
    except (OSError,ValueError,KeyError,TypeError,PcError):pass
    try:
        from core.home.device_controls import validate_device_targets
        p=Path(os.getenv('RIO_HOME_DEVICE_TARGET_FILE',str(Path.home()/'.config/rio/google-home-devices.json')))
        devices=validate_device_targets(json.loads(read_private(p))) if p.exists() else {}
        if devices.get('enabled'):
            for target,binding in devices['targets'].items():
                tools.append(spec('home.device.'+target,'등록된 '+target+'의 허용 기능만. 온도 변경·새 기능 추정 금지.', 'control',action=enum(*binding['allowed_commands'])))
    except (OSError,ValueError,KeyError,TypeError,PcError):pass
    if os.getenv('RIO_WEB_SEARCH_ENABLED','true').lower() not in {'1','true','yes','on'}:
        tools=[t for t in tools if t['name']!='web.search']
    return {tool['name']:tool for tool in tools}

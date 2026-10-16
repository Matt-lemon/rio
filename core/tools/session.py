"""An explicit validation session, enabled only in the independent checkout."""
import json
import threading
import time
import uuid
from .contracts import ToolError
from .main_adapter import handle_tools_command
from .verification import SrtVerification

def status_message(result,request_id=None):
    state=result.get('status');key=request_id or result.get('id')
    if state=='WAITING_MODEL':
        return '모델 연결을 확인하지 못해 제작을 대기 중이야. 코드는 아직 생성하지 않았어.'+(' 요청 ID: '+key if key else '')
    if state in {'QUEUED','GENERATING','TESTING','RUNNING'}:
        descriptions={'QUEUED':'요청을 접수했어.','GENERATING':'실제 모델로 코드를 생성 중이야.',
                      'TESTING':'생성 코드를 Docker에서 검사 중이야.','RUNNING':'등록한 도구를 실행 중이야.'}
        return descriptions[state]+(' 요청 ID: '+key if key else '')
    return json.dumps(result,ensure_ascii=False)

def run_session(reader=input,writer=print):
    writer('Rio: 제작 검증 세션이 준비됐어. "리오, SRT 검사 도구 제작 검증을 시작해줘"라고 요청하면 시작할게. /exit 로 나갈 수 있어.')
    stopped=threading.Event();tasks=[]
    def watch(flow):
        deadline=time.monotonic()+600
        try:
            while not stopped.is_set() and time.monotonic()<deadline:
                result=flow.poll()
                if result['status'] not in {'QUEUED','GENERATING','TESTING','RUNNING'}:
                    writer('Rio: '+status_message(result,flow.key));return
                stopped.wait(2)
            if not stopped.is_set():writer('Rio: 검증이 아직 진행 중이야. /tools result '+flow.key+' 로 다시 확인해줘.')
        except ToolError as e:writer('Rio: 검증 중단: '+e.code)
        except Exception:writer('Rio: 검증 결과를 확인하지 못했어. 완료로 보고하지 않을게.')
    try:
        while True:
            try:text=reader('You: ').strip()
            except (EOFError,KeyboardInterrupt):break
            if text in {'/exit','/quit'}:break
            if text=='리오, SRT 검사 도구 제작 검증을 시작해줘':
                text='/tools verify user_srt_'+uuid.uuid4().hex
            try:
                if text.startswith('/tools verify ') or text.startswith('/tools resume '):
                    flow=SrtVerification(text.split(None,2)[2])
                    result=(flow.resume()
                            if text.startswith('/tools resume ') else flow.start())
                    writer('Rio: '+status_message(result,flow.key))
                    if result['status'] in {'QUEUED','GENERATING','TESTING','ACTIVE'}:
                        task=threading.Thread(target=watch,args=(flow,),daemon=True);tasks.append(task);task.start()
                elif text.startswith('/tools result '):
                    flow=SrtVerification(text.split(None,2)[2])
                    result=flow.poll()
                    writer('Rio: '+status_message(result,flow.key))
                elif text:
                    reply=handle_tools_command(text)
                    writer('Rio: '+(reply or '제작 검증은 /tools verify 요청ID 로 요청해줘.'))
            except ToolError as e:writer('Rio: 요청 실패: '+e.code)
            except Exception:writer('Rio: 서비스 연결이나 입력을 확인해줘.')
    finally:
        stopped.set()
        for task in tasks:task.join(timeout=3)

"""Human-triggered SRT verification; IPC only, no host execution or approvals."""
import json
import re
from .client import ToolClient
from .contracts import LOW_RISK,ToolError

SAMPLE={'text':'1\n00:00:00,000 --> 00:00:01,000\nfixed validation sample\n'}
EXPECTED={'segments':1,'valid_segments':1,'total_duration_ms':1000,
          'timeline_duration_ms':1000,'invalid_times':[],'overlap_count':0}

class SrtVerification:
    def __init__(self,request_id,client=None):
        if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}',request_id):
            raise ToolError('INVALID_REQUEST_ID')
        self.key=request_id;self.client=client or ToolClient()

    def request(self,action,**args):
        reply=self.client.request(action,**args)
        if reply.get('ok') is not True:raise ToolError(reply.get('code','SERVICE_REJECTED'))
        return reply['result']

    def start(self):
        found=self.request('list',search='srt_inspect')
        if any(item.get('name')=='srt_inspect' for item in found):
            return {'status':'EXISTING_TOOL_FOUND','code':'USE_SEPARATE_EMPTY_VALIDATION_REGISTRY'}
        return self.request('build',request_id=self.key,capability='srt.readonly.inspect')

    def resume(self):
        existing=self.request('job',request_id=self.key)
        if existing['status'] not in {'WAITING_MODEL','QUEUED'}:return existing
        return self.request('build',request_id=self.key,capability='srt.readonly.inspect')

    def poll(self):
        build=self.request('job',request_id=self.key)
        if build['status'] not in {'ACTIVE','REUSED'}:return {'status':build['status'],'build':build}
        attempts=build.get('attempts',[])
        if not attempts or not any(a.get('package_hash')==build['package_hash'] and
                re.fullmatch('[0-9a-f]{64}',a.get('response_hash') or '') for a in attempts):
            return {'status':'UNVERIFIED','code':'REAL_MODEL_PROVENANCE_REQUIRED','build':build}
        tool=self.request('describe',name='srt_inspect')
        approval=json.loads(tool['approval']) if isinstance(tool.get('approval'),str) else tool.get('approval',{})
        if (tool['hash']!=build['package_hash'] or tool['state']!='ACTIVE' or
                not set(tool['contract']['permissions'])<=LOW_RISK or approval.get('package_hash')!=tool['hash']):
            raise ToolError('REGISTERED_VERSION_OR_APPROVAL_MISMATCH')
        calls=[]
        for number in (1,2):
            key=self.key+'_call_'+str(number)
            self.request('call',name='srt_inspect',args=SAMPLE,request_id=key)
            result=self.request('job',request_id=key)
            if result['status']!='SUCCEEDED':return {'status':result['status'],'phase':'call_'+str(number),'job':result}
            if result['package_hash']!=tool['hash'] or result['result']['result']!=EXPECTED:
                raise ToolError('POST_REGISTRATION_TEST_FAILED')
            calls.append(result)
        return {'status':'SRT_VALIDATION_COMPLETED','build':build,'package_hash':tool['hash'],
                'image_id':tool['contract']['image_id'],'approval':approval,
                'validation':self.request('validation',name='srt_inspect'),'calls':calls}

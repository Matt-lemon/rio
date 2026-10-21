import ast
import re
from core.tools.contracts import ToolError, validate_package, validate_value
from .srt import fixed_tests

IMPORTS={'json','sys','re','heapq','math','statistics','collections','itertools','decimal','datetime'}
FORBIDDEN={'eval','exec','compile','__import__','getattr','setattr','globals','locals','vars','open','input','breakpoint'}


def static_check(package):
    c=validate_package(package)
    source=package['files'][c['entrypoint']]
    if c['kind']=='python':
        try:tree=ast.parse(source)
        except SyntaxError as error:
            # Parse only. Keep bounded compiler diagnostics tied to this source;
            # never execute an invalid candidate to discover its failure.
            lines=source.splitlines();line=error.lineno or 1
            failure=ToolError('PYTHON_SYNTAX')
            failure.diagnostic={'line':line,'column':error.offset,
                'message':error.msg[:240],
                'context':[{'line':i+1,'text':lines[i][:240]}
                    for i in range(max(0,line-3),min(len(lines),line+2))]}
            raise failure from None
        for node in ast.walk(tree):
            if isinstance(node,ast.Import) and any(a.name not in IMPORTS for a in node.names):
                raise ToolError('IMPORT_NOT_ALLOWED')
            if isinstance(node,ast.ImportFrom) and (node.level or node.module not in IMPORTS):
                raise ToolError('IMPORT_NOT_ALLOWED')
            if isinstance(node,ast.Name) and node.id in FORBIDDEN:
                raise ToolError('DYNAMIC_EXECUTION_NOT_ALLOWED')
            if isinstance(node,ast.Attribute) and node.attr.startswith('__'):
                raise ToolError('DYNAMIC_EXECUTION_NOT_ALLOWED')
    elif re.search(r'`|\$\(|\b(eval|source|sudo|docker|curl|wget|mount|nc)\b|\b(?:ba)?sh\s+-c|/(?:home|proc|var/run)/',source):
        raise ToolError('BASH_STATIC_REJECTED')


ISOLATION_SOURCE='''import json,os,socket
blocked=False
s=socket.socket();s.settimeout(.5)
try:s.connect(('1.1.1.1',443))
except OSError:blocked=True
finally:s.close()
readonly=False
try:open('/opt/rio/forbidden-write','w').write('no')
except OSError:readonly=True
status=open('/proc/self/status').read()
print(json.dumps({'nonroot':os.getuid()!=0,'network_blocked':blocked,'root_readonly':readonly,
 'no_docker_socket':not os.path.exists('/var/run/docker.sock'),
 'no_host_home':not os.path.exists('/home/example'),
 'no_credentials':not any(x in os.environ for x in ['LLM_API_KEY','RIO_DUBBING_TOKEN','SMARTTHINGS_TOKEN']),
 'caps_empty':'CapEff:\\t0000000000000000' in status,
 'no_new_privs':'NoNewPrivs:\\t1' in status}))
'''


class Validator:
    def __init__(self,registry,executor):
        self.registry=registry;self.executor=executor

    def validate(self,key):
        package=self.registry.load_package(key)
        report={'static_pass':False,'fixed_tests_pass':False,'isolation_pass':False,'test_count':0}
        try:
            static_check(package);report['static_pass']=True
            if package['contract']['capability']!='srt.readonly.inspect':
                raise ToolError('INDEPENDENT_ORACLE_REQUIRED')
            for args,expected in fixed_tests():
                result=self.executor.run_package(package,args,lane='builder')['result']
                schema=package['contract']['output_schema']
                try:validate_value(schema,result,'INVALID_OUTPUT')
                except ToolError:
                    # Describe only contract field failures. Never send oracle
                    # inputs, expected answers or returned values to the model.
                    diagnostic={'stage':'output_schema','message':'Program stdout must match the published metrics schema; no kind/code wrapper.',
                        'missing_fields':[],'unexpected_fields':[],'invalid_fields':[]}
                    if isinstance(result,dict):
                        diagnostic['missing_fields']=[k for k in schema['required'] if k not in result]
                        diagnostic['unexpected_fields']=[k[:80] for k in result if k not in schema['properties']][:16]
                        for field,field_schema in schema['properties'].items():
                            if field in result:
                                try:validate_value(field_schema,result[field],'INVALID_OUTPUT')
                                except ToolError:diagnostic['invalid_fields'].append(field)
                    else:diagnostic['message']='Program stdout must be a metrics JSON object.'
                    report['diagnostic']=diagnostic
                    raise ToolError('INVALID_OUTPUT') from None
                if result!=expected:
                    report['diagnostic']={'stage':'fixed_test','message':'Metrics differ from the published SRT specification. Review timestamp bounds, invalid ranges, ordinal segment numbers and all overlapping pairs.',
                        'mismatched_fields':[k for k in expected if result[k]!=expected[k]]}
                    raise ToolError('FIXED_TEST_FAILED')
                report['test_count']+=1
            report['fixed_tests_pass']=True
            isolation=self.executor.execute({'mode':'package','kind':'python','source':ISOLATION_SOURCE,'input':{}},lane='builder')['result']
            report['isolation']=isolation
            report['isolation_pass']=isolation=={k:True for k in ('nonroot','network_blocked','root_readonly',
                'no_docker_socket','no_host_home','no_credentials','caps_empty','no_new_privs')}
            if not report['isolation_pass']:raise ToolError('ISOLATION_TEST_FAILED')
        except ToolError as e:
            report['error_code']=e.code
            if e.code=='PYTHON_SYNTAX' and hasattr(e,'diagnostic'):
                report['diagnostic']=e.diagnostic
        self.registry.validated(key,report)
        return report

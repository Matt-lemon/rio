"""Reviewed template and independent fixed oracle. Originals are never read."""
import textwrap

SOURCE=textwrap.dedent('''\
import heapq
import json
import re
import sys

text=json.load(sys.stdin)['text'].replace('\\r\\n','\\n').replace('\\r','\\n')
blocks=[b for b in re.split(r'\\n\\s*\\n',text.strip()) if b.strip()]
timing=re.compile(r'^(\\d{2,4}):(\\d{2}):(\\d{2}),(\\d{3}) --> (\\d{2,4}):(\\d{2}):(\\d{2}),(\\d{3})$')
valid=[]
invalid=[]
for number,block in enumerate(blocks,1):
    lines=block.strip().splitlines()
    m=timing.fullmatch(lines[1].strip()) if len(lines)>=2 and lines[0].strip().isdigit() else None
    if m is None:
        invalid.append({'segment':number,'code':'FORMAT'})
        continue
    a=list(map(int,m.groups()))
    start=((a[0]*60+a[1])*60+a[2])*1000+a[3]
    end=((a[4]*60+a[5])*60+a[6])*1000+a[7]
    if any(a[i]>=60 for i in (1,2,5,6)) or end<=start:
        invalid.append({'segment':number,'code':'INVALID_RANGE'})
        continue
    valid.append((start,end))
active=[]
overlaps=0
for start,end in sorted(valid):
    while active and active[0]<=start:
        heapq.heappop(active)
    overlaps+=len(active)
    heapq.heappush(active,end)
print(json.dumps({'segments':len(blocks),'valid_segments':len(valid),
 'total_duration_ms':sum(end-start for start,end in valid),
 'timeline_duration_ms':max((end for _,end in valid),default=0),
 'invalid_times':invalid,'overlap_count':overlaps}))
''')

INPUT={'type':'object','properties':{'text':{'type':'string','maxLength':1048576}},
       'required':['text'],'additionalProperties':False}
NUM={'type':'integer','minimum':0}
OUTPUT={'type':'object','properties':{
    'segments':NUM,'valid_segments':NUM,'total_duration_ms':NUM,'timeline_duration_ms':NUM,
    'overlap_count':NUM,'invalid_times':{'type':'array','maxItems':32768,'items':{
        'type':'object','properties':{'segment':{'type':'integer','minimum':1},
        'code':{'type':'string','enum':['FORMAT','INVALID_RANGE']}},
        'required':['segment','code'],'additionalProperties':False}}},
    'required':['segments','valid_segments','total_duration_ms','timeline_duration_ms','invalid_times','overlap_count'],
    'additionalProperties':False}


def package(image_id,source=SOURCE,version='1.0.0',kind='python'):
    entry='tool.py' if kind=='python' else 'tool.sh'
    return {'contract':{'name':'srt_inspect','description':'Read-only SRT timing inspection; sum of valid durations and maximum end timestamp in milliseconds.',
        'version':version,'input_schema':INPUT,'output_schema':OUTPUT,'kind':kind,
        'entrypoint':entry,'image_id':image_id,'permissions':['input.read','workspace.compute'],
        'capability':'srt.readonly.inspect'},'files':{entry:source}}


def fixed_tests():
    return [({'text':''},{'segments':0,'valid_segments':0,'total_duration_ms':0,'timeline_duration_ms':0,'invalid_times':[],'overlap_count':0}),
     ({'text':'1\n00:00:01,000 --> 00:00:03,000\nA\n\n2\n00:00:02,000 --> 00:00:04,000\nB'},
      {'segments':2,'valid_segments':2,'total_duration_ms':4000,'timeline_duration_ms':4000,'invalid_times':[],'overlap_count':1}),
     ({'text':'1\n00:00:04,000 --> 00:00:02,000\nA\n\n2\n00:61:00,000 --> 00:62:00,000\nB\n\n3\nbad\nC'},
      {'segments':3,'valid_segments':0,'total_duration_ms':0,'timeline_duration_ms':0,
       'invalid_times':[{'segment':1,'code':'INVALID_RANGE'},{'segment':2,'code':'INVALID_RANGE'},{'segment':3,'code':'FORMAT'}],'overlap_count':0}),
     ({'text':'1\n00:00:00,000 --> 00:00:10,000\nA\n\n2\n00:00:01,000 --> 00:00:09,000\nB\n\n3\n00:00:02,000 --> 00:00:03,000\nC'},
      {'segments':3,'valid_segments':3,'total_duration_ms':19000,'timeline_duration_ms':10000,'invalid_times':[],'overlap_count':3})]

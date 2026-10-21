"""Bounded, best-effort shadow retrieval. Never participates in reply selection."""
import hashlib,json,os,queue,threading,time,uuid
from pathlib import Path
import httpx

class ShadowRecorder:
    def __init__(self,url,log_path,timeout=2,capacity=16):
        self.url=url.rstrip('/');self.path=Path(log_path);self.timeout=timeout
        self.queue=queue.Queue(maxsize=capacity);self.run_id=uuid.uuid4().hex;self.dropped=0
        self.thread=threading.Thread(target=self._run,name='rio-memory-shadow',daemon=True);self.thread.start()
    @staticmethod
    def compact(rows):return [{'id':str(r['id']),'rank':i+1,'score':float(r.get('score',0))} for i,r in enumerate(rows)]
    def submit(self,query,bge_rows,latency_ms,limit,cutoff):
        event={'query_id':hashlib.sha256(query.encode()).hexdigest(),'run_id':self.run_id,'time':time.time(),'bge':self.compact(bge_rows),'bge_latency_ms':latency_ms,'bge_cutoff':cutoff,'limit':limit}
        try:self.queue.put_nowait((query,event))
        except queue.Full:self.dropped+=1
    def _write(self,event):
        # Query/content are deliberately omitted. Operational failures are isolated.
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.exists() and self.path.stat().st_size>10*1024**2:
            os.replace(self.path,self.path.with_suffix('.previous.jsonl'))
        fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a') as stream:stream.write(json.dumps(event)+'\n')
    def _run(self):
        while True:
            query,event=self.queue.get()
            try:
                begin=time.perf_counter()
                with httpx.Client(timeout=self.timeout) as client:
                    response=client.post(self.url+'/search',json={'query':query,'limit':event['limit']})
                    response.raise_for_status();event['gemma']=self.compact(response.json()['results'])
                event['gemma_latency_ms']=(time.perf_counter()-begin)*1000;event['status']='ok'
            except Exception as error:
                event['status']='error';event['error_type']=type(error).__name__
                event['gemma_latency_ms']=(time.perf_counter()-begin)*1000
            event['dropped_total']=self.dropped
            try:self._write(event)
            except Exception:pass
            finally:self.queue.task_done()

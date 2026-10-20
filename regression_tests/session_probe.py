import asyncio, json, os, shutil, subprocess, sys, tempfile, unittest; from contextlib import asynccontextmanager; from pathlib import Path; from types import SimpleNamespace; import httpx; Msg=type('ExternalMessage',(),{'__init__':lambda self,**kw:self.__dict__.update(kw)})
async def legacy_reply(msg): return SimpleNamespace(layout_info={'one':('LocalCPUBackend',2048)}) if hasattr(msg,'tokens') else SimpleNamespace(instance_id={'a':'one','b':'two'}[msg.ip])
controller=SimpleNamespace(LMCacheControllerManager=lambda *args,**kw:SimpleNamespace(handle_orchestration_message=legacy_reply)); sys.modules.update({'transformers':SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a,**kw:SimpleNamespace(encode=lambda prompt:list(range(len(prompt)))))), 'lmcache':SimpleNamespace(), 'lmcache.v1':SimpleNamespace(), 'lmcache.v1.cache_controller':SimpleNamespace(controller_manager=controller), 'lmcache.v1.cache_controller.message':SimpleNamespace(LookupMsg=Msg,QueryInstMsg=Msg)})
from serving_lab.fabric import FabricGateway; from vllm_router.deadline import ManualClock
class Transport:
    def __init__(self, rows, config): self.rows={r['id']:r for r in rows}; self.config=config; self.sent=[]; self.waiting=set(); self.gates={(rid,p):asyncio.Event() for rid,row in self.rows.items() for p in ['prefill','transfer','first','end']+['frame:'+str(i) for i in range(len(row.get('decode_frames',[])))]}
    async def body(self, rid):
        if 'decode_frames' in self.rows[rid]:
            for i,frame in enumerate(self.rows[rid]['decode_frames']): p='frame:'+str(i); self.waiting.add((rid,p)); await self.gates[rid,p].wait(); wire=frame['wire'].encode(); yield wire[:13]; yield wire[13:]
            return
        yield b': alive\n\ndata: {"choices":[{"text":""}]}\n\n'; self.waiting.add((rid,'first')); await self.gates[rid,'first'].wait(); wire='data: {"choices":[{"text":"甲"}]}\n\n'.encode(); yield wire[:30]; yield wire[30:]; self.waiting.add((rid,'end')); await self.gates[rid,'end'].wait()
        if self.rows[rid].get('fail_phase')=='decode': raise OSError('decode failed')
        yield b'data: [DONE]\n\n'
        if self.rows[rid].get('tail_after_done'): yield b'data: {"choices":[{"text":"late"}]}\n\n'
    @asynccontextmanager
    async def request(self, **kw):
        headers={k.lower():v for k,v in kw['headers'].items()}; rid=headers['x-request-id']; r=kw['url'].split('/')[2]; role=self.config['resources'][r]['role']; p='transfer' if role=='link' else role; row=self.rows[rid]; timing=row['timing'][p]; call=dict(id=rid,resource=r,url=kw['url'],body=kw['json']); expected=row.get('backend_deployments',{}).get(r)
        if expected: call['deployment']=dict(deployment_id=headers.get('x-deployment-id',kw['json'].get('target_deployment_id')),generation=int(headers.get('x-deployment-generation',kw['json'].get('target_generation',-1))),kv_schema=headers.get('x-kv-schema',kw['json'].get('kv_schema')))
        self.sent.append(call)
        if expected and call['deployment']!=expected: raise ValueError('backend deployment fence mismatch')
        headers={'x-service-sample':timing['sample'],'x-service-us':str(timing['service_us']),'x-baseline-us':'1'}
        if p=='decode': yield SimpleNamespace(status=200,headers=headers,content=SimpleNamespace(iter_any=lambda:self.body(rid))); return
        self.waiting.add((rid,p)); await self.gates[rid,p].wait()
        if row.get('fail_phase')==p: raise OSError('phase unavailable')
        ack=dict(request_id=rid,resource=r,layout=self.config['resources'][r]['layout'],kv_handle=f'{rid}:{r}:kv')
        if expected: ack.update(expected); ack['generation']+=int(row.get('bad_deployment')==p)
        if p=='prefill': ack['cached_tokens']=kw['json']['cached_tokens']
        if p=='transfer': ack['target']=kw['json']['target']
        if row.get('bad_ack')==p: ack['request_id']='other-request'
        if row.get('bad_handle')==p: ack['kv_handle']='foreign-handle'
        async def payload(): return ack
        yield SimpleNamespace(status=200,headers=headers,json=payload)
def client_output(content): data=['\n'.join(line[5:].lstrip(' ') for line in event.split('\n') if line.startswith('data:')) for event in content.decode().replace('\r\n','\n').split('\n\n')]; rows=[json.loads(p) for p in data if p and p!='[DONE]']; assert all(r.get('kind') not in ['begin','page','kv_block','query','shard','commit'] for r in rows),'raw tensor event leaked to client'; return dict(text=''.join(c.get('text','') for r in rows for c in r.get('choices',[])),token_ids=[t for r in rows for c in r.get('choices',[]) for t in c.get('token_ids',[])],done='[DONE]' in data)
async def settle(predicate):
    async with asyncio.timeout(5):
        while not predicate(): await asyncio.sleep(0)
"""Independent public HTTP driver for the paged-session contract.

The prefix with Transport and compatibility import shims is copied from the
previous independent transport. Neither part imports an author solution.
"""


async def run_session(source, segmentation='split', checkpoint_fields=None):
    work=json.loads((source/'workload.json').read_text())
    config=json.loads((source/'fabric.json').read_text())
    clock=ManualClock(work['window'][0])
    backend=Transport(work['requests'],config)
    if segmentation=='whole':
        async def whole_body(rid):
            for i,frame in enumerate(backend.rows[rid]['decode_frames']):
                key=(rid,'frame:'+str(i));backend.waiting.add(key)
                await backend.gates[key].wait()
                yield frame['wire'].encode()
        backend.body=whole_body
    gateway=FabricGateway(source,source/'profiles'/work['initial'],clock=clock,transport=backend)
    tasks={};events=[];checkpoints=[];reads=[]
    for row in work['requests']:
        events.append((row['at_us'],8,row['id'],'arrival',row))
        for rank,kind in [(0,'cancel'),(3,'transfer'),(4,'prefill')]:
            if kind+'_us' in row:events.append((row[kind+'_us'],rank,row['id'],kind,row))
        events.extend((frame['at_us'],2,row['id'],'frame:'+str(i),row) for i,frame in enumerate(row['decode_frames']))
    events.extend((r['at_us'],5,str(i),'reload',r) for i,r in enumerate(work['reloads']))
    events.extend((r['at_us'],1,str(i).zfill(6),'read',r['body']) for i,r in enumerate(work.get('kv_reads',[])))
    events.extend((t,9,str(i),'checkpoint',{}) for i,t in enumerate(work['checkpoints']))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app,raise_app_exceptions=False),base_url='http://gateway') as client:
        try:
            for at,_,_,kind,row in sorted(events,key=lambda e:e[:3]):
                clock.advance_to(at);rid=row.get('id')
                if kind=='arrival':
                    tasks[rid]=asyncio.create_task(client.post('/v1/completions',json=row['body'],headers={'X-Request-Id':rid}))
                    if not any(r['at_us']==at and r['id']>rid for r in work['requests']):
                        await settle(lambda:all(t.done() or any(k[0]==r for k in backend.waiting) for r,t in tasks.items()))
                elif kind=='cancel' and rid in tasks:
                    tasks[rid].cancel();await asyncio.gather(tasks[rid],return_exceptions=True)
                elif kind=='read':
                    reads.append((await client.post('/fabric/kv-reads',json=row)).status_code)
                elif kind=='reload':
                    await gateway.reload(source/'profiles'/row['build'])
                elif kind=='checkpoint':
                    state=(await client.get('/fabric/diagnostics')).json()
                    observed=dict(at_us=at,pages={r:n['pages'] for r,n in state['nodes'].items()},
                        paged_attention=state.get('paged_attention',{}),outcomes={r:n['outcome'] for r,n in state['requests'].items()})
                    if checkpoint_fields is not None:
                        required=checkpoint_fields[at]
                        for field in ['paged_attention','outcomes']:
                            observed[field]={k:v for k,v in observed[field].items() if k in required[field]}
                        conforms(observed,required,'$/checkpoints/'+str(len(checkpoints)))
                    checkpoints.append(observed)
                elif rid in tasks and not tasks[rid].done():
                    backend.gates[rid,kind].set()
                    next_phase={'prefill':'transfer','transfer':'frame:0'}.get(kind)
                    if kind.startswith('frame:'):
                        index=int(kind.split(':')[1])+1
                        next_phase='frame:'+str(index) if index<len(row['decode_frames']) else None
                    await settle(lambda:tasks[rid].done() or next_phase is not None and (rid,next_phase) in backend.waiting)
            replies=await asyncio.wait_for(asyncio.gather(*tasks.values(),return_exceptions=True),5)
            statuses={rid:r.status_code if isinstance(r,httpx.Response) else 'cancelled' if isinstance(r,asyncio.CancelledError) else 'transport-error' for rid,r in zip(tasks,replies)}
            outputs={rid:client_output(r.content) for rid,r in zip(tasks,replies) if isinstance(r,httpx.Response) and r.status_code==200}
            # Independently observe real dispatch. No candidate-provided replay is read.
            for rid,status in statuses.items():
                sent=[r for r in backend.sent if r['id']==rid]
                if status in [200,'cancelled']:
                    assert len(sent)==3,(rid,'missing/duplicate HTTP phase')
                    assert [r['url'].rsplit('/',1)[-1] for r in sent]==['prefill','kv-transfer','completions']
                else:assert not sent,(rid,'rejected request dispatched')
            result=dict(statuses=statuses,outputs=outputs,checkpoints=checkpoints)
            if 'kv_reads' in work:result['reads']=reads
            return result
        finally:
            for task in tasks.values():task.cancel()
            await asyncio.gather(*tasks.values(),return_exceptions=True)


def conforms(actual,expected,path='$'):
    if isinstance(expected,dict):
        assert isinstance(actual,dict),path+' must be an object'
        for key,value in expected.items():
            assert key in actual,path+'/'+str(key)+' missing'
            conforms(actual[key],value,path+'/'+str(key))
    elif isinstance(expected,list):
        assert isinstance(actual,list) and len(actual)==len(expected),path+' length mismatch'
        for i,(a,e) in enumerate(zip(actual,expected)):conforms(a,e,path+'/'+str(i))
    elif isinstance(expected,float):
        assert isinstance(actual,(int,float)) and not isinstance(actual,bool) and abs(actual-expected)<=1e-5,path+' numerical mismatch'
    else:assert type(actual)==type(expected) and actual==expected,path+' contract mismatch: '+repr(actual)+' != '+repr(expected)


async def verify_case(source,expected,segmentation='split'):
    actual=await asyncio.wait_for(run_session(source,segmentation,{r['at_us']:r for r in expected['checkpoints']}),90)
    conforms(actual,expected)
    return actual

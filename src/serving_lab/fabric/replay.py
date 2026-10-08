"""Investigative phase-aware CPU replay over the real completions ASGI entry."""
import asyncio
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
from vllm_router.deadline import ManualClock
from serving_lab.storage import read_json, write_json
from .compile import compile_fabric
from .runtime import FabricGateway


class Backend:
    def __init__(self, rows, config):
        self.rows = {row['id']:row for row in rows}
        self.config = config
        self.calls, self.waiting, self.passed = [], set(), set()
        self.gates = {(rid,phase):asyncio.Event() for rid,row in self.rows.items() for phase in ['prefill','transfer','first','end']+['frame:'+str(i) for i in range(len(row.get('decode_frames',[])))]}

    async def body(self, rid):
        if 'decode_frames' in self.rows[rid]:
            for index, frame in enumerate(self.rows[rid]['decode_frames']):
                phase='frame:'+str(index); self.waiting.add((rid,phase)); await self.gates[rid,phase].wait()
                wire=frame['wire'].encode()
                for offset in range(0,len(wire),7): yield wire[offset:offset+7]
            return
        yield b': heartbeat\n\ndata: {"choices":[{"text":""}]}\n\n'
        self.waiting.add((rid,'first'))
        await self.gates[rid,'first'].wait()
        wire = 'data: {"choices":[{"text":"甲"}]}\n\n'.encode()
        yield wire[:30]
        yield wire[30:]
        self.passed.add((rid,'first'))
        self.waiting.add((rid,'end'))
        await self.gates[rid,'end'].wait()
        if self.rows[rid].get('fail_phase') == 'decode':
            raise OSError('decode stream failed')
        yield b'data: [DONE]\n\n'
        if self.rows[rid].get('tail_after_done'):
            yield b'data: {"choices":[{"text":"late"}]}\n\n'
        self.passed.add((rid,'end'))

    @asynccontextmanager
    async def request(self, **kw):
        normalized_headers = {k.lower():v for k,v in kw['headers'].items()}
        rid = normalized_headers['x-request-id']
        resource = kw['url'].split('/')[2]
        role = self.config['resources'][resource]['role']
        phase = 'transfer' if role == 'link' else role
        row = self.rows[rid]
        call = dict(id=rid,resource=resource,url=kw['url'],body=kw['json'])
        expected = row.get('backend_deployments', {}).get(resource)
        if expected:
            actual = dict(deployment_id=normalized_headers.get('x-deployment-id',kw['json'].get('target_deployment_id')),
                          generation=int(normalized_headers.get('x-deployment-generation',kw['json'].get('target_generation',-1))),
                          kv_schema=normalized_headers.get('x-kv-schema',kw['json'].get('kv_schema')))
            call['deployment'] = actual
            self.calls.append(call)
            if actual != expected: raise ValueError('backend deployment fence mismatch')
        else:
            self.calls.append(call)
        timing = row['timing'][phase]
        headers = {'x-service-sample':timing['sample'],'x-service-us':str(timing['service_us']), 'x-baseline-us':'1'}
        if phase != 'decode':
            self.waiting.add((rid,phase))
            await self.gates[rid,phase].wait()
            if row.get('fail_phase') == phase:
                raise OSError('phase failed')
            ack = dict(request_id=rid,resource=resource,layout=self.config['resources'][resource]['layout'],kv_handle=f'{rid}:{resource}:kv')
            if expected:
                ack.update(expected)
                if row.get('bad_deployment') == phase: ack['generation'] += 1
            if phase == 'prefill':
                ack['cached_tokens'] = kw['json']['cached_tokens']
            if phase == 'transfer':
                ack['target'] = kw['json']['target']
            if row.get('bad_ack') == phase:
                ack['request_id'] = 'other-request'
            if row.get('bad_handle') == phase:
                ack['kv_handle'] = 'foreign-handle'
            async def json_body():
                return ack
            self.passed.add((rid,phase))
            yield SimpleNamespace(status=200,headers=headers,json=json_body)
        else:
            yield SimpleNamespace(status=200,headers=headers,content=SimpleNamespace(iter_any=lambda:self.body(rid)))


async def settle(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


def events(work):
    output = []
    for rank,field in [(0,'cancel'),(1,'end'),(2,'first'),(3,'transfer'),(4,'prefill')]:
        output.extend((r[field+'_us'],rank,r['id'],field,r) for r in work['requests'] if r.get(field+'_us') is not None)
    for rank,field in [(5,'reloads'),(6,'observations'),(7,'caches')]:
        output.extend((r['at_us'],rank,str(i),field,r) for i,r in enumerate(work[field]))
    output.extend((r['at_us'],8,r['id'],'arrival',r) for r in work['requests'])
    output.extend((t,9,str(i),'checkpoint',{}) for i,t in enumerate(work['checkpoints']))
    output.extend((f['at_us'],2,r['id'],'frame:'+str(i),r) for r in work['requests'] for i,f in enumerate(r.get('decode_frames',[])))
    return sorted(output,key=lambda e:e[:3])


async def replay(source, workload, output):
    source, output = Path(source), Path(output)
    work = read_json(workload)
    dirs = {}
    for row in work['builds']:
        dirs[row['name']] = output/'profiles'/row['name']
        compile_fabric(source,dirs[row['name']],row['as_of_us'])
    temporary = tempfile.TemporaryDirectory(prefix='fabric-replay-input-')
    source = Path(shutil.copytree(source,Path(temporary.name)/'source'))
    backend = Backend(work['requests'],read_json(source/'fabric.json'))
    clock = ManualClock(work['window'][0])
    gateway = FabricGateway(source,dirs[work['initial']],clock=clock,transport=backend)
    tasks, checkpoints = {}, []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app,raise_app_exceptions=False),base_url='http://gateway') as client:
        try:
            for at,rank,_,kind,row in events(work):
                clock.advance_to(at)
                rid = row.get('id')
                if kind == 'arrival':
                    tasks[rid] = asyncio.create_task(client.post('/v1/completions',json=row['body'],headers={'X-Request-Id':rid}))
                    if not any(r['at_us'] == at and r['id'] > rid for r in work['requests']):
                        await settle(lambda:all(task.done() or any(key[0] == identity for key in backend.waiting) for identity,task in tasks.items()))
                elif (kind in ['prefill','transfer','first','end'] or kind.startswith('frame:')) and rid in tasks and not tasks[rid].done():
                    backend.gates[rid,kind].set()
                    next_phase = dict(prefill='transfer',transfer='first',first='end').get(kind)
                    if kind=='transfer' and 'decode_frames' in row: next_phase='frame:0' if row['decode_frames'] else None
                    if kind.startswith('frame:'):
                        index=int(kind.split(':')[1])+1; next_phase='frame:'+str(index) if index<len(row['decode_frames']) else None
                    await settle(lambda:tasks[rid].done() or (next_phase is not None and (rid,next_phase) in backend.waiting))
                elif kind == 'cancel' and rid in tasks:
                    tasks[rid].cancel()
                    await asyncio.gather(tasks[rid],return_exceptions=True)
                elif kind == 'reloads':
                    if row.get('asset_override'):
                        asset_path=source/backend.config['decode_assets']; original=asset_path.read_bytes()
                        override=row['asset_override']; catalogue=json.loads(original); target=catalogue[override['identity']]
                        for part in override['path'][:-1]: target=target[part]
                        target[override['path'][-1]]=override['value']; asset_path.write_text(json.dumps(catalogue))
                        try:
                            try: await gateway.reload(dirs[row['build']])
                            except Exception: pass
                            else: raise AssertionError('invalid assets accepted')
                        finally: asset_path.write_bytes(original)
                    else:
                        await gateway.reload(dirs[row['build']])
                elif kind == 'observations':
                    gateway.observe(row['rows'])
                elif kind == 'caches':
                    gateway.cache(row['leases'])
                elif kind == 'checkpoint':
                    checkpoints.append(dict(at_us=at,**(await client.get('/fabric/diagnostics')).json(),dispatch=copy_calls(backend.calls)))
            replies = await asyncio.gather(*tasks.values(),return_exceptions=True)
            result = dict(checkpoints=checkpoints,statuses={rid:r.status_code if isinstance(r,httpx.Response) else 'cancelled' for rid,r in zip(tasks,replies)})
            if any('decode_frames' in r for r in work['requests']):
                result['outputs']={rid:client_output(response.content) for rid,response in zip(tasks,replies) if isinstance(response,httpx.Response) and response.status_code==200}
            write_json(output/'fabric-evaluation.json',result)
            output.mkdir(exist_ok=True,parents=True)
            (output/'metrics.prom').write_text((await client.get('/metrics')).text,encoding='utf-8')
            return result
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(),return_exceptions=True)
            temporary.cleanup()


def copy_calls(calls):
    return sorted(calls,key=lambda row:(row['id'],row['url']))


def client_output(content):
    text=''; tokens=[]; done=False
    for event in content.decode().replace('\r\n','\n').split('\n\n'):
        data='\n'.join(line[5:].lstrip() for line in event.split('\n') if line.startswith('data:'))
        if data=='[DONE]': done=True
        elif data:
            row=json.loads(data)
            for choice in row.get('choices',[]):
                text+=choice.get('text',''); tokens.extend(choice.get('token_ids',[]))
    return dict(text=text,token_ids=tokens,done=done)

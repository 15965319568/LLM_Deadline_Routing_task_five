"""Public structured-inference observations through independent HTTP peers."""
import asyncio
import copy
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from serving_lab.fabric import FabricGateway, compile_fabric
from vllm_router.deadline import ManualClock

SOURCE=Path(__file__).resolve().parents[1]/'captures/structured-7'
WORK=json.loads((SOURCE/'workload.json').read_text(encoding='utf-8'))
EXPECTED=json.loads((Path(__file__).parent/'data/constraint-observations.json').read_text(encoding='utf-8'))


def conforms(actual,expected):
    if isinstance(expected,dict):
        assert isinstance(actual,dict) and expected.keys()<=actual.keys()
        for key,value in expected.items(): conforms(actual[key],value)
    elif isinstance(expected,list):
        assert isinstance(actual,list) and len(actual)==len(expected)
        for a,b in zip(actual,expected): conforms(a,b)
    elif isinstance(expected,float): assert abs(actual-expected)<=1e-5
    else: assert actual==expected


def output(content):
    rows=[]; done=False
    for event in content.decode().replace('\r\n','\n').split('\n\n'):
        data='\n'.join(line[5:].lstrip(' ') for line in event.split('\n') if line.startswith('data:'))
        if data=='[DONE]': done=True
        elif data: rows.append(json.loads(data))
    return dict(text=''.join(c.get('text','') for r in rows for c in r.get('choices',[])),
                token_ids=[t for r in rows for c in r.get('choices',[]) for t in c.get('token_ids',[])],done=done)


class Peer:
    def __init__(self,row,config,clock,chunk):
        self.row,self.config,self.clock,self.chunk=row,config,clock,chunk
        self.calls=[]; self.gw=None; self.inspect=None; self.reload=None
        self.blocked=asyncio.Event(); self.release=asyncio.Event()
    async def stream(self):
        for index,frame in enumerate(self.row['decode_frames']):
            if index: await self.inspect(index-1)
            if self.row.get('cancel_us') is not None and frame['at_us']>=self.row['cancel_us']:
                self.blocked.set(); await self.release.wait()
            self.clock.advance_to(frame['at_us']); await self.reload()
            wire=frame['wire'].encode()
            for offset in range(0,len(wire),self.chunk): yield wire[offset:offset+self.chunk]
    @asynccontextmanager
    async def request(self,**kw):
        self.calls.append(kw); rid=self.row['id']; resource=kw['url'].split('/')[2]
        headers={k.lower():v for k,v in kw['headers'].items()}; expected=self.row['backend_deployments'][resource]
        assert headers.get('x-deployment-id',kw['json'].get('target_deployment_id'))==expected['deployment_id']
        assert int(headers.get('x-deployment-generation',kw['json'].get('target_generation',-1)))==expected['generation']
        if kw['url'].endswith('/v1/completions'):
            if self.row.get('backend_grammar'):
                assert headers.get('x-grammar-id',kw['json'].get('grammar_id'))==self.row['backend_grammar']['grammar_id']
                assert int(headers.get('x-grammar-revision',kw['json'].get('grammar_revision',-1)))==self.row['backend_grammar']['revision']
            yield SimpleNamespace(status=200,headers={},content=SimpleNamespace(iter_any=self.stream)); return
        phase='prefill' if kw['url'].endswith('/v1/prefill') else 'transfer'
        self.clock.advance_to(self.row[phase+'_us']); await self.reload()
        ack=dict(expected,request_id=rid,resource=resource,layout=self.config['resources'][resource]['layout'],kv_handle=f'{rid}:{resource}:opaque')
        if phase=='prefill': ack['cached_tokens']=kw['json']['cached_tokens']
        else: ack['target']=kw['json']['target']
        async def payload(): return ack
        yield SimpleNamespace(status=200,headers={},json=payload)


@pytest.fixture(scope='module')
def profiles(tmp_path_factory):
    root=tmp_path_factory.mktemp('constraint-profiles')
    for row in WORK['builds']: compile_fabric(SOURCE,root/row['name'],row['as_of_us'])
    return root


@pytest.mark.parametrize('name',['initial','rotated','grammar'])
def test_raw_grammar_reconciliation(profiles,name):
    actual=json.loads((profiles/name/'grammar-profile.json').read_text())
    conforms(actual,EXPECTED['builds'][name])


@pytest.mark.parametrize('chunk',[1,4096])
@pytest.mark.parametrize('rid',sorted(EXPECTED['requests']))
def test_stateful_constrained_inference(profiles,rid,chunk):
    row=copy.deepcopy(next(r for r in WORK['requests'] if r['id']==rid)); expected=EXPECTED['requests'][rid]
    async def run():
        initial='initial' if row['at_us']<104100 else 'rotated' if row['at_us']<106100 else 'grammar'
        clock=ManualClock(row['at_us']); peer=Peer(row,json.loads((SOURCE/'fabric.json').read_text()),clock,chunk)
        gateway=FabricGateway(SOURCE,profiles/initial,clock=clock,transport=peer)
        pending=[event for event in WORK['reloads'] if row['at_us']<event['at_us']<=row['decode_frames'][-1]['at_us']]
        async def rotate():
            while pending and pending[0]['at_us']<=clock.now_us():
                event=pending.pop(0)
                if not event.get('profile_override'):
                    await gateway.reload(profiles/event['build'])
        peer.reload=rotate
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app,raise_app_exceptions=False),base_url='http://gateway') as client:
            async def inspect(index):
                state=(await client.get('/fabric/diagnostics')).json()
                if str(index) in expected['frames']:
                    conforms(state['decoding'][rid],expected['frames'][str(index)])
            peer.inspect=inspect
            task=asyncio.create_task(client.post('/v1/completions',json=row['body'],headers={'X-Request-Id':rid}))
            try:
                if row.get('cancel_us'):
                    await asyncio.wait_for(peer.blocked.wait(),5); task.cancel(); await asyncio.gather(task,return_exceptions=True)
                else:
                    response=await asyncio.wait_for(task,8); assert response.status_code==expected['status']
                    if response.status_code!=200:
                        assert not peer.calls; return
                    conforms(output(response.content),expected['output'])
                state=(await client.get('/fabric/diagnostics')).json()
                assert state['requests'][rid]['outcome']==expected['outcome']
                conforms(state['speculation'][rid],expected['stats']); conforms(state['decoding'][rid],expected['state'])
                assert all(n['slots']==n['pages']==n['work_us']==0 for n in state['nodes'].values())
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_invalid_complete_grammar_reload_is_atomic(profiles,tmp_path):
    directory=tmp_path/'bad'; shutil.copytree(profiles/'grammar',directory)
    p=directory/'grammar-profile.json'; data=json.loads(p.read_text()); data['grammars']['answer']['edges'][0]['to']=999999; p.write_text(json.dumps(data))
    async def run():
        gateway=FabricGateway(SOURCE,profiles/'grammar',clock=ManualClock(220000),transport=object())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://gateway') as client:
            before=(await client.get('/fabric/diagnostics')).json()
            with pytest.raises(Exception): await gateway.reload(directory)
            conforms((await client.get('/fabric/diagnostics')).json(),before)
    asyncio.run(run())

"""Public inference behavior probes; independent transport, public raw input only."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from serving_lab.fabric import FabricGateway, compile_fabric
from vllm_router.deadline import ManualClock

SOURCE=Path(__file__).resolve().parents[1]/'captures/speculative-7'
WORK=json.loads((SOURCE/'workload.json').read_text(encoding='utf-8'))
OBSERVATIONS=json.loads((Path(__file__).parent/'data/spec-observations.json').read_text(encoding='utf-8'))


class Peer:
    def __init__(self,row,config,clock):
        self.row=row; self.config=config; self.clock=clock; self.calls=[]; self.before_frame=None; self.before_transfer=None
        self.blocked=asyncio.Event(); self.release=asyncio.Event()
    async def stream(self):
        for index,frame in enumerate(self.row['decode_frames']):
            if self.row.get('cancel_us') is not None and frame['at_us']>=self.row['cancel_us']:
                self.blocked.set(); await self.release.wait()
            self.clock.advance_to(frame['at_us'])
            if self.before_frame: await self.before_frame(index)
            wire=frame['wire'].encode()
            for i in range(0,len(wire),2): yield wire[i:i+2]
    @asynccontextmanager
    async def request(self,**kw):
        self.calls.append(kw); rid=self.row['id']; resource=kw['url'].split('/')[2]
        expected=self.row['backend_deployments'][resource]
        headers={k.lower():v for k,v in kw['headers'].items()}
        assert headers.get('x-deployment-id',kw['json'].get('target_deployment_id'))==expected['deployment_id']
        assert int(headers.get('x-deployment-generation',kw['json'].get('target_generation',-1)))==expected['generation']
        if kw['url'].endswith('/v1/completions'):
            yield SimpleNamespace(status=200,headers={},content=SimpleNamespace(iter_any=self.stream)); return
        phase='prefill' if kw['url'].endswith('/v1/prefill') else 'transfer'
        self.clock.advance_to(self.row[phase+'_us'])
        if phase=='transfer' and self.before_transfer: await self.before_transfer()
        ack=dict(expected,request_id=rid,resource=resource,layout=self.config['resources'][resource]['layout'],kv_handle=f'{rid}:{resource}:observed')
        if phase=='prefill': ack['cached_tokens']=kw['json']['cached_tokens']
        else: ack['target']=kw['json']['target']
        async def payload(): return ack
        yield SimpleNamespace(status=200,headers={},json=payload)


def output(content):
    records=[]
    for event in content.decode().replace('\r\n','\n').split('\n\n'):
        data='\n'.join(line[5:].lstrip(' ') for line in event.split('\n') if line.startswith('data:'))
        if data and data!='[DONE]':
            row=json.loads(data); assert row.get('kind') not in ['begin','shard','commit'],'backend tensors leaked to client'; records.append(row)
    return dict(text=''.join(c.get('text','') for r in records for c in r.get('choices',[])),
                token_ids=[t for r in records for c in r.get('choices',[]) for t in c.get('token_ids',[])],
                done=b'data: [DONE]' in content)


@pytest.mark.parametrize('rid',sorted(OBSERVATIONS))
def test_quantized_partitioned_decode_through_production_http(tmp_path,rid):
    row=copy.deepcopy(next(r for r in WORK['requests'] if r['id']==rid))
    cutoff=100700 if rid=='q00' else 104100
    compile_fabric(SOURCE,tmp_path/'initial',cutoff); compile_fabric(SOURCE,tmp_path/'rotated',104100)
    async def run():
        clock=ManualClock(row['at_us']); peer=Peer(row,json.loads((SOURCE/'fabric.json').read_text()),clock)
        gw=FabricGateway(SOURCE,tmp_path/'initial',clock=clock,transport=peer)
        if rid=='q00':
            async def rotate():
                await gw.reload(tmp_path/'rotated')
            peer.before_transfer=rotate
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app,raise_app_exceptions=False),base_url='http://gateway') as client:
            response=await client.post('/v1/completions',json=row['body'],headers={'X-Request-Id':rid})
            expected=OBSERVATIONS[rid]; assert response.status_code==expected['status']
            if response.status_code==400:
                assert not peer.calls; return
            actual=output(response.content)
            for field,value in expected['output'].items(): assert actual[field]==value
            state=(await client.get('/fabric/diagnostics')).json()
            assert state['requests'][rid]['outcome']==expected['outcome']
            assert state['requests'][rid]['held']==[]
            for key,value in expected['stats'].items(): assert state['speculation'][rid][key]==value
            assert all(n['slots']==n['pages']==n['work_us']==0 for n in state['nodes'].values())
    asyncio.run(run())


def test_cancel_with_incomplete_tensor_window_releases_draft_pages(tmp_path):
    row=copy.deepcopy(next(r for r in WORK['requests'] if r['id']=='q06'))
    compile_fabric(SOURCE,tmp_path,104100)
    async def run():
        clock=ManualClock(row['at_us']); peer=Peer(row,json.loads((SOURCE/'fabric.json').read_text()),clock)
        gw=FabricGateway(SOURCE,tmp_path,clock=clock,transport=peer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app),base_url='http://gateway') as client:
            task=asyncio.create_task(client.post('/v1/completions',json=row['body'],headers={'X-Request-Id':row['id']}))
            try:
                await asyncio.wait_for(peer.blocked.wait(),5)
                held=(await client.get('/fabric/diagnostics')).json()
                assert held['speculation'][row['id']]['scratch_pages']==1
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
                final=(await client.get('/fabric/diagnostics')).json()
                assert final['speculation'][row['id']]['scratch_pages']==0
                assert final['requests'][row['id']]['outcome']=='cancelled'
                assert all(n['pages']==n['slots']==0 for n in final['nodes'].values())
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('cutoff',[100000,100700])
def test_evidenced_cache_and_real_attempts_survive_cleaning(tmp_path,cutoff):
    built=compile_fabric(SOURCE,tmp_path,cutoff)
    samples={(r['resource'],r['sample_id'],r['attempt']) for r in built['phase-ledger']['samples']}
    assert ('p2','s23-166',0) in samples,'service-time valid cache evidence was discarded'
    assert ('p2','s23-149',0) not in samples,'compile-phase observation contaminated steady service'
    assert ('p2','s23-165',0) not in samples,'measurement from another deployment was accepted'
    assert built['fabric-profiles']['resources']['p2']['support'][1]==7
    assert built['phase-audit']['qualified_samples']==len(samples)

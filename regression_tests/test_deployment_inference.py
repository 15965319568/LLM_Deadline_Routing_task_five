"""Public behavior probes for rolling LLM deployments; no private fixture imports."""
import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from serving_lab.fabric import FabricGateway, compile_fabric
from vllm_router.deadline import ManualClock

SOURCE=Path(__file__).resolve().parents[1]/'captures/fabric-7'

def build(tmp_path, cutoff=100700):
    return compile_fabric(SOURCE,tmp_path,cutoff)

def body(now=104000, **changes):
    value=dict(model='lab-model',prompt='甲'*8,max_tokens=4,deadline_us=now+3000,stream=True,
               model_revision='rev-2026-10',tokenizer_revision='tok-2026-10',quantization='fp8',kv_schema='q8',adapter='chat')
    value.update(changes)
    return value

class Peer:
    def __init__(self, deployments, bad=None, hold=False):
        self.deployments=copy.deepcopy(deployments); self.bad=bad; self.calls=[]
        self.arrived=asyncio.Event(); self.release=asyncio.Event()
        if not hold: self.release.set()
    async def stream(self):
        wire='data: {"choices":[{"text":"甲"}]}\n\ndata: [DONE]\n\n'.encode()
        for i in range(0,len(wire),7): yield wire[i:i+7]
    @asynccontextmanager
    async def request(self, **kw):
        resource=kw['url'].split('/')[2]; self.calls.append(kw); rid=kw['headers']['X-Request-Id']; expected=self.deployments[resource]
        actual=kw['headers'].get('X-Deployment-Id',kw['json'].get('target_deployment_id'))
        assert actual==expected['deployment_id'], 'dispatch used a deployment other than the transaction snapshot'
        if kw['url'].endswith('/v1/completions'):
            yield SimpleNamespace(status=200,headers={},content=SimpleNamespace(iter_any=self.stream)); return
        if kw['url'].endswith('/v1/prefill'):
            self.arrived.set(); await self.release.wait()
        ack={f:expected[f] for f in ('deployment_id','generation','kv_schema')}
        ack.update(request_id=rid,resource=resource,layout=expected['kv_schema'],kv_handle=f'{rid}:{resource}:proof')
        if kw['url'].endswith('/v1/prefill'): ack['cached_tokens']=kw['json']['cached_tokens']
        else: ack['target']=kw['json']['target']
        if self.bad and kw['url'].endswith(self.bad): ack['generation']+=1
        async def payload(): return ack
        yield SimpleNamespace(status=200,headers={'x-service-sample':rid+resource,'x-service-us':'800'},json=payload)

def test_rollout_requires_three_consistent_exports_and_changes_generation(tmp_path):
    before=build(tmp_path/'old'); after=build(tmp_path/'new',104100)
    for r in ['p3','l33','d3']:
        assert before['deployment-profile']['resources'][r]['generation']==2
        assert after['deployment-profile']['resources'][r]['generation']==3
        assert before['fabric-profiles']['resources'][r]==after['fabric-profiles']['resources'][r]
    states={r['disposition'] for r in after['deployment-profile']['ledger']}
    assert {'candidate','excluded','invalid','duplicate','conflict','deferred'}<=states

def test_cost_equal_rollout_advances_only_changed_epochs_and_is_idempotent(tmp_path):
    build(tmp_path/'old'); build(tmp_path/'new',104100)
    async def run():
        gw=FabricGateway(SOURCE,tmp_path/'old',clock=ManualClock(104200)); before=gw.app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=before),base_url='http://gateway') as client:
            old=(await client.get('/fabric/diagnostics')).json(); await gw.reload(tmp_path/'new'); new=(await client.get('/fabric/diagnostics')).json()
            for r in old['nodes']: assert new['nodes'][r]['epoch']==old['nodes'][r]['epoch']+int(r in ['p3','l33','d3'])
            await gw.reload(tmp_path/'new'); assert (await client.get('/fabric/diagnostics')).json()==new
    asyncio.run(run())

@pytest.mark.parametrize('phase,call_count',[('/v1/prefill',1),('/v1/kv-transfer',2)])
def test_wrong_deployment_ack_stops_downstream_and_releases_everything(tmp_path,phase,call_count):
    profile=build(tmp_path)
    async def run():
        peer=Peer(profile['deployment-profile']['resources'],bad=phase); gw=FabricGateway(SOURCE,tmp_path,clock=ManualClock(104000),transport=peer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app),base_url='http://gateway') as client:
            assert (await client.post('/v1/completions',json=body(),headers={'X-Request-Id':'fence'})).status_code==502
            state=(await client.get('/fabric/diagnostics')).json(); assert state['requests']['fence']['held']==[]
            assert state['outcomes']['error']==1 and len(peer.calls)==call_count
            assert all(n['slots']==n['pages']==n['work_us']==0 for n in state['nodes'].values())
    asyncio.run(run())

def test_inflight_transaction_keeps_old_deployments_across_reload(tmp_path):
    old=build(tmp_path/'old'); build(tmp_path/'new',104100)
    async def run():
        peer=Peer(old['deployment-profile']['resources'],hold=True); clock=ManualClock(104000); gw=FabricGateway(SOURCE,tmp_path/'old',clock=clock,transport=peer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app),base_url='http://gateway') as client:
            task=asyncio.create_task(client.post('/v1/completions',json=body(),headers={'X-Request-Id':'rolling'}))
            await asyncio.wait_for(peer.arrived.wait(),5); clock.advance_to(104200); await gw.reload(tmp_path/'new'); peer.release.set()
            response=await asyncio.wait_for(task,5); assert response.status_code==200 and b'[DONE]' in response.content
            state=(await client.get('/fabric/diagnostics')).json(); assert state['requests']['rolling']['outcome']=='success'
            assert all(state['nodes'][r]['factor']==1 for r in ['p3','l33','d3']), 'old deployment feedback polluted new epoch'
            assert len(peer.calls)==3
    asyncio.run(run())

@pytest.mark.parametrize('field',['model_revision','tokenizer_revision','quantization','adapter','kv_schema'])
def test_valid_but_incompatible_selector_cannot_fall_back(tmp_path,field):
    build(tmp_path)
    async def run():
        peer=Peer({}); gw=FabricGateway(SOURCE,tmp_path,clock=ManualClock(104000),transport=peer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app),base_url='http://gateway') as client:
            assert (await client.post('/v1/completions',json=body(**{field:'unsupported'}),headers={'X-Request-Id':'selector'})).status_code==503
            assert peer.calls==[]
    asyncio.run(run())

def test_same_hash_old_generation_cannot_prove_new_prefill_cache(tmp_path):
    profile=build(tmp_path,104100); dep=profile['deployment-profile']['resources']['p3']
    lease=dict(lease_id='cache-proof',resource='p3',layout='q8',tokens='甲'*4,page_index=0,session_id='',producer='cache-peer',
               valid_from_us=104000,expires_us=108000,ingested_us=104000,page_hash=hashlib.sha256('q8||0|甲甲甲甲'.encode()).hexdigest(),
               **{f:dep[f] for f in ('deployment_id','generation','model_revision','tokenizer_revision','quantization','kv_schema','adapter')})
    async def run():
        for generation,cached in [(2,0),(3,4)]:
            peer=Peer(profile['deployment-profile']['resources']); gw=FabricGateway(SOURCE,tmp_path,clock=ManualClock(104200),transport=peer)
            gw.cache([dict(lease,generation=generation)])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app),base_url='http://gateway') as client:
                assert (await client.post('/v1/completions',json=body(),headers={'X-Request-Id':'cache'})).status_code==200
                assert peer.calls[0]['json']['cached_tokens']==cached
    asyncio.run(run())

def test_invalid_deployment_reload_is_atomic(tmp_path):
    build(tmp_path/'old'); build(tmp_path/'new',104100)
    path=tmp_path/'new/deployment-profile.json'; document=json.loads(path.read_text()); document['resources']['l33']['generation']=-1; path.write_text(json.dumps(document))
    async def run():
        gw=FabricGateway(SOURCE,tmp_path/'old',clock=ManualClock(104200))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app),base_url='http://gateway') as client:
            before=(await client.get('/fabric/diagnostics')).json()
            with pytest.raises(ValueError): await gw.reload(tmp_path/'new')
            assert (await client.get('/fabric/diagnostics')).json()==before
    asyncio.run(run())

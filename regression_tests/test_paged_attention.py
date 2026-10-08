"""Public paged inference examples: real HTTP, frame boundaries and continued admission."""
import asyncio
import copy
import json
import shutil
from pathlib import Path
import httpx
import pytest
from serving_lab.fabric import FabricGateway, compile_fabric
from serving_lab.fabric.replay import replay
from vllm_router.deadline import ManualClock
from test_constraint_serving import Peer, conforms, output

SOURCE=Path(__file__).resolve().parents[1]/'captures/paged-7'
WORK=json.loads((SOURCE/'workload.json').read_text(encoding='utf-8'))
EXPECTED=json.loads((Path(__file__).parent/'data/paged-observations.json').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def profiles(tmp_path_factory):
    root=tmp_path_factory.mktemp('paged-profiles')
    for row in WORK['builds']: compile_fabric(SOURCE,root/row['name'],row['as_of_us'])
    return root


@pytest.mark.parametrize('chunk',[1,4096])
@pytest.mark.parametrize('rid',sorted(EXPECTED['requests']))
def test_paged_http_windows(profiles,rid,chunk):
    row=copy.deepcopy(next(r for r in WORK['requests'] if r['id']==rid)); expected=EXPECTED['requests'][rid]
    async def run():
        initial='initial' if row['at_us']<104100 else 'rotated' if row['at_us']<106100 else 'grammar'
        clock=ManualClock(row['at_us']); peer=Peer(row,json.loads((SOURCE/'fabric.json').read_text()),clock,chunk)
        gateway=FabricGateway(SOURCE,profiles/initial,clock=clock,transport=peer)
        pending=[event for event in WORK['reloads'] if row['at_us']<event['at_us']<=row['decode_frames'][-1]['at_us']]
        async def rotate():
            while pending and pending[0]['at_us']<=clock.now_us():
                event=pending.pop(0)
                if not event.get('asset_override'): await gateway.reload(profiles/event['build'])
        peer.reload=rotate
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app,raise_app_exceptions=False),base_url='http://gateway') as client:
            async def inspect(index):
                state=(await client.get('/fabric/diagnostics')).json()
                if str(index) in expected['frames']:
                    for key,value in expected['frames'][str(index)].items(): conforms(state[key][rid],value)
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
                    for frame in response.content.decode().replace('\r\n','\n').split('\n\n'):
                        data='\n'.join(line[5:].lstrip(' ') for line in frame.split('\n') if line.startswith('data:'))
                        if data and data!='[DONE]': assert json.loads(data).get('kind') not in ['begin','page','query','shard','commit']
                state=(await client.get('/fabric/diagnostics')).json()
                assert state['requests'][rid]['outcome']==expected['outcome']
                for key,field in [('speculation','stats'),('decoding','state'),('paged_attention','paged')]:
                    conforms(state[key][rid],expected[field])
                assert all(n['slots']==n['pages']==n['work_us']==0 for n in state['nodes'].values())
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_continued_arrivals_observe_workspace(tmp_path):
    actual=asyncio.run(replay(SOURCE,SOURCE/'workload.json',tmp_path/'replay'))
    conforms(actual['statuses'],EXPECTED['continuous']['statuses'])
    by_time={c['at_us']:c for c in actual['checkpoints']}
    for checkpoint in EXPECTED['continuous']['checkpoints']: conforms(by_time[checkpoint['at_us']],checkpoint)


def test_invalid_attention_reload_is_atomic(profiles,tmp_path):
    source=tmp_path/'source'; shutil.copytree(SOURCE,source)
    async def run():
        gateway=FabricGateway(source,profiles/'grammar',clock=ManualClock(210000),transport=object())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://gateway') as client:
            before=(await client.get('/fabric/diagnostics')).json()
            path=source/'decode-assets.json'; assets=json.loads(path.read_text())
            assets['d3-blue-r3']['paged_attention']['head_map'][0]=999
            path.write_text(json.dumps(assets))
            with pytest.raises(Exception): await gateway.reload(profiles/'grammar')
            conforms((await client.get('/fabric/diagnostics')).json(),before)
    asyncio.run(run())

"""Public sustained-state probes using HTTP and independent backend barriers.

These checks use only the documented Gateway, build outputs, diagnostics and
metrics. They do not inspect implementation attributes or call transition helpers.
"""
import asyncio
import copy
import json
import math
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from serving_lab.fabric import FabricGateway, compile_fabric
from vllm_router.deadline import ManualClock

SOURCE = Path(__file__).resolve().parents[1] / 'captures/speculative-7'
ROUTE = ['p3', 'l33', 'd3']
COSTS = dict(zip(ROUTE, [100, 40, 60]))


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def setup_case(tmp_path, *, budget=300, cutoff=104100):
    source = Path(shutil.copytree(SOURCE, tmp_path / 'input'))
    config = read(source / 'fabric.json')
    config.update(paths=[ROUTE], priority_factors=[1, 1.5], route_quarantine_us=0)
    config['tenant_limits'] = {'default': dict(max_slots=20, max_pages=200, max_priority=1)}
    if budget is not None:
        config['tenant_limits']['default']['max_work_us'] = budget
    for key in ROUTE:
        config['resources'][key]['slots'] = 20
    config['resources']['d3']['pages'] = 200
    write(source / 'fabric.json', config)
    profile = tmp_path / 'profile'
    compile_fabric(source, profile, cutoff)
    # A legal profile update makes each budget boundary independently calculable:
    # admission work = 100 + 40 + 60 = 200, irrespective of candidate fitting code.
    costs = read(profile / 'fabric-profiles.json')
    for key, cost in COSTS.items():
        costs['resources'][key]['service_us'] = [cost] * len(costs['resources'][key]['axis'])
        costs['resources'][key]['support'] = [config['minimum_samples']] * len(costs['resources'][key]['axis'])
    write(profile / 'fabric-profiles.json', costs)
    deployments = read(profile / 'deployment-profile.json')['resources']
    body = dict(model=config['model'], prompt='甲' * 12, max_tokens=2,
                deadline_us=200000, stream=True, tenant='default', priority=0)
    body.update({k: deployments['d3'][k] for k in
                 ['model_revision', 'tokenizer_revision', 'quantization', 'adapter', 'kv_schema']})
    return source, profile, config, deployments, body


class BarrierPeer:
    def __init__(self, config, deployments, *, frames=None, chunk=13, ending='success'):
        self.config, self.deployments = config, deployments
        self.frames, self.chunk, self.ending = frames, chunk, ending
        self.calls = []
        self.boundaries = asyncio.Queue()
        self.inflight = None

    async def pause(self, rid, label):
        if rid == 'inflight':
            gate = asyncio.Event()
            await self.boundaries.put((label, gate))
            await gate.wait()

    async def at(self, label):
        pending = asyncio.create_task(self.boundaries.get())
        try:
            done, _ = await asyncio.wait([pending, self.inflight], timeout=5, return_when=asyncio.FIRST_COMPLETED)
            if pending not in done:
                if self.inflight in done:
                    response = self.inflight.result()
                    raise AssertionError(f'request ended before {label}: HTTP {response.status_code}, body={response.text[:300]!r}')
                raise AssertionError(f'backend did not reach {label}; calls={[(r, s) for r, s, _ in self.calls]}')
            actual, gate = pending.result()
            assert actual == label, f'expected backend boundary {label}, reached {actual}'
            return gate
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def stream(self, rid):
        frames = self.frames if rid == 'inflight' and self.frames is not None else [
            'data: {"choices":[{"text":"甲"}]}\n\n', 'data: [DONE]\n\n']
        for index, wire in enumerate(frames):
            await self.pause(rid, f'frame:{index}')
            if rid == 'inflight' and self.ending == 'error' and index == len(frames) - 1:
                raise OSError('backend lost before DONE')
            raw = wire.encode('utf-8')
            for offset in range(0, len(raw), self.chunk):
                yield raw[offset:offset+self.chunk]
        await self.pause(rid, 'eof')

    @asynccontextmanager
    async def request(self, **kw):
        headers = {k.lower(): v for k, v in kw['headers'].items()}
        rid, resource = headers['x-request-id'], kw['url'].split('/')[2]
        spec = self.config['resources'][resource]
        self.calls.append((rid, resource, copy.deepcopy(kw['json'])))
        deployment = self.deployments[resource]
        # Both documented fence representations are accepted; no JSON extras
        # or header casing are imposed on implementations.
        assert headers.get('x-deployment-id', kw['json'].get('target_deployment_id')) == deployment['deployment_id']
        assert int(headers.get('x-deployment-generation', kw['json'].get('target_generation', -1))) == deployment['generation']
        if spec['role'] == 'decode':
            yield SimpleNamespace(status=200, headers={}, content=SimpleNamespace(iter_any=lambda: self.stream(rid)))
            return
        phase = 'transfer' if spec['role'] == 'link' else 'prefill'
        await self.pause(rid, phase)
        ack = dict(deployment, request_id=rid, resource=resource, layout=spec['layout'], kv_handle=f'{rid}:{resource}:lease')
        ack.update(cached_tokens=kw['json']['cached_tokens']) if phase == 'prefill' else ack.update(target=kw['json']['target'])

        async def payload():
            return ack
        yield SimpleNamespace(status=200, headers={}, json=payload)


def client_for(gateway):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app, raise_app_exceptions=False), base_url='http://gateway')


async def post(client, body, rid):
    return await asyncio.wait_for(client.post('/v1/completions', json=body, headers={'X-Request-Id': rid}), 5)


async def state(client):
    return (await client.get('/fabric/diagnostics')).json()


async def check_metrics(client, diagnostics):
    samples = [s for f in text_string_to_metric_families((await client.get('/metrics')).text) for s in f.samples]
    for field in ['slots', 'pages', 'work_us']:
        actual = {s.labels['resource']: s.value for s in samples if s.name == 'fabric_' + field}
        for resource, node in diagnostics['nodes'].items():
            assert actual[resource] == pytest.approx(node[field], abs=1e-5)
    required = {'fabric_'+f for f in ['slots','pages','work_us','factor','epoch','ttft_seconds_count','ttft_seconds_sum','terminal_total']}
    required.update('fabric_speculative_'+f for f in ['proposed','accepted','corrected','committed','windows','scratch_pages'])
    assert all(set(s.labels) <= {'resource', 'outcome'} for s in samples if s.name in required)


@pytest.mark.parametrize('ending', ['success', 'error', 'cancelled'])
@pytest.mark.parametrize('priority', [0, 1])
def test_tenant_bill_survives_physical_release_until_terminal(tmp_path, ending, priority):
    source, profile, config, deployments, body = setup_case(tmp_path)
    body['priority'] = priority

    async def run():
        clock = ManualClock(105000)
        peer = BarrierPeer(config, deployments, ending=ending)
        gateway = FabricGateway(source, profile, clock=clock, transport=peer)
        async with client_for(gateway) as client:
            task = peer.inflight = asyncio.create_task(post(client, body, 'inflight'))
            try:
                for index, (boundary, held, work) in enumerate([
                    ('prefill', ROUTE, [100, 40, 60]),
                    ('transfer', ROUTE[1:], [0, 40, 60]),
                    ('frame:0', ROUTE[2:], [0, 0, 60]),
                    ('frame:1', ROUTE[2:], [0, 0, 0]),
                ]):
                    gate = await peer.at(boundary)
                    snap = await state(client)
                    assert snap['requests']['inflight']['held'] == sorted(held)
                    for resource, expected_work in zip(ROUTE, work):
                        assert snap['nodes'][resource]['work_us'] == pytest.approx(expected_work), 'priority must not scale physical service'
                        assert snap['nodes'][resource]['slots'] == int(resource in held)
                    assert snap['nodes']['d3']['pages'] == 4
                    assert snap['requests']['inflight']['first_us'] == (105040 if boundary == 'frame:1' else None)
                    await check_metrics(client, snap)
                    before = len(peer.calls)
                    competitor = await post(client, dict(body, priority=0), f'probe-{boundary}')
                    assert competitor.status_code == 429, f'{boundary}: unfinished tenant bill must survive physical work release'
                    assert len(peer.calls) == before, 'rejected request was dispatched'
                    if ending == 'cancelled' and boundary == 'frame:1':
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        break
                    clock.advance_to(105010 + index * 15)
                    gate.set()
                if ending == 'success':
                    (await peer.at('eof')).set()
                if ending != 'cancelled':
                    assert (await task).status_code == 200
                final = await state(client)
                assert final['requests']['inflight']['outcome'] == ending
                assert final['outcomes'].get(ending, 0) == 1
                assert all(n['slots'] == n['pages'] == n['work_us'] == 0 for n in final['nodes'].values())
                assert (await post(client, body, 'after-terminal')).status_code == 200, 'terminal did not return the fixed tenant bill exactly once'
                await check_metrics(client, await state(client))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_reload_keeps_old_bill_and_uses_new_cost_for_later_admission(tmp_path):
    source, profile, config, deployments, body = setup_case(tmp_path, budget=500)
    replacement = Path(shutil.copytree(profile, tmp_path / 'replacement'))
    costs = read(replacement / 'fabric-profiles.json')
    for resource in ROUTE:
        costs['resources'][resource]['service_us'] = [2 * n for n in costs['resources'][resource]['service_us']]
    write(replacement / 'fabric-profiles.json', costs)

    async def run():
        clock = ManualClock(105000)
        peer = BarrierPeer(config, deployments)
        gateway = FabricGateway(source, profile, clock=clock, transport=peer)
        async with client_for(gateway) as client:
            task = peer.inflight = asyncio.create_task(post(client, body, 'inflight'))
            try:
                for label in ['prefill', 'transfer', 'frame:0']:
                    (await peer.at(label)).set()
                end = await peer.at('frame:1')
                previous = (await state(client))['requests']['inflight']
                await gateway.reload(replacement)
                snap = await state(client)
                for field in ['baseline_us', 'reserved_us', 'epochs', 'first_us', 'pages']:
                    assert snap['requests']['inflight'][field] == previous[field], f'reload changed in-flight {field}'
                assert (await post(client, body, 'over-budget')).status_code == 429, 'old 200 + new 400 exceeds budget 500'
                end.set()
                (await peer.at('eof')).set()
                await task
                assert (await post(client, body, 'new-cost')).status_code == 200
                snap = await state(client)
                for resource, expected in zip(ROUTE, [200, 80, 120]):
                    assert snap['requests']['new-cost']['reserved_us'][resource] == pytest.approx(expected)
                assert snap['decisions']['new-cost']['predicted_us'] == 400 + config['safety_us']
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('chunk', [1, 2, 13, 4096])
@pytest.mark.parametrize('ending', ['success', 'cancelled'])
def test_tensor_windows_remain_observable_before_text_and_at_commit(tmp_path, chunk, ending):
    source, profile, config, deployments, body = setup_case(tmp_path, budget=None)
    row = next(r for r in read(source / 'workload.json')['requests'] if r['id'] == 'q05')
    body = dict(row['body'], deadline_us=200000)
    frames = [f['wire'].replace('"request_id": "q05"', '"request_id": "inflight"') for f in row['decode_frames']]

    async def run():
        clock = ManualClock(row['at_us'])
        peer = BarrierPeer(config, deployments, frames=frames, chunk=chunk)
        gateway = FabricGateway(source, profile, clock=clock, transport=peer)
        async with client_for(gateway) as client:
            task = peer.inflight = asyncio.create_task(post(client, body, 'inflight'))
            try:
                for label in ['prefill', 'transfer']:
                    (await peer.at(label)).set()
                expected_proposed = 0
                expected_committed = 0
                scratch = 0
                first_time = None
                for index, wire in enumerate(frames):
                    gate = await peer.at(f'frame:{index}')
                    snap = await state(client)
                    stats = snap['speculation']['inflight']
                    assert stats['proposed'] == expected_proposed
                    assert stats['committed'] == expected_committed, f'frame {index}: output became committed before commit'
                    assert stats['scratch_pages'] == scratch
                    assert snap['nodes']['d3']['pages'] == 4 + scratch
                    assert snap['requests']['inflight']['first_us'] == first_time, 'token submission is not necessarily first UTF-8 text'
                    assert snap['nodes']['d3']['work_us'] == (60 if first_time is None else 0)
                    await check_metrics(client, snap)
                    if ending == 'cancelled' and expected_committed == 1 and scratch:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        break
                    clock.advance_to(row['decode_frames'][index]['at_us'])
                    payload = wire.split('data:', 1)[1].strip()
                    if payload != '[DONE]':
                        event = json.loads(payload)
                        if event['kind'] == 'begin':
                            expected_proposed += len(event['proposal'])
                            scratch = math.ceil(len(event['proposal']) / config['page_tokens'])
                        if event['kind'] == 'commit':
                            expected_committed += 1  # q05 commits one UTF-8 fragment in each window.
                            scratch = 0
                            if expected_committed == 2:
                                first_time = clock.now_us()
                    gate.set()
                if ending == 'success':
                    (await peer.at('eof')).set()
                    response = await task
                    assert response.status_code == 200
                    payloads = ['\n'.join(line[5:].lstrip(' ') for line in e.split('\n') if line.startswith('data:'))
                                for e in response.text.replace('\r\n', '\n').split('\n\n')]
                    events = [json.loads(data) for data in payloads if data and data != '[DONE]']
                    assert ''.join(c.get('text', '') for e in events for c in e.get('choices', [])) == '甲'
                snap = await state(client)
                assert snap['requests']['inflight']['outcome'] == ending
                assert snap['speculation']['inflight']['scratch_pages'] == 0
                assert snap['speculation']['inflight']['committed'] == (2 if ending == 'success' else 1)
                assert snap['ttft_count'] == (1 if ending == 'success' else 0)
                assert all(n['pages'] == n['slots'] == n['work_us'] == 0 for n in snap['nodes'].values())
                # The same resource remains usable after partial UTF-8 cancellation.
                ordinary = {k: v for k, v in body.items() if k not in ['decode_mode', 'temperature', 'top_p', 'top_k']}
                assert (await post(client, ordinary, 'following')).status_code == 200
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_scratch_exhaustion_releases_bill_but_quarantines_partial_kv_path(tmp_path):
    source, profile, config, deployments, body = setup_case(tmp_path)
    config['resources']['d3']['pages'] = 4  # Admission fits; one scratch page does not.
    config['route_quarantine_us'] = 240
    write(source / 'fabric.json', config)
    row = next(r for r in read(source / 'workload.json')['requests'] if r['id'] == 'q05')
    spec_body = dict(row['body'], deadline_us=200000)
    frames = [f['wire'].replace('"request_id": "q05"', '"request_id": "inflight"') for f in row['decode_frames']]

    async def run():
        clock = ManualClock(111400)
        peer = BarrierPeer(config, deployments, frames=frames)
        gateway = FabricGateway(source, profile, clock=clock, transport=peer)
        async with client_for(gateway) as client:
            task = peer.inflight = asyncio.create_task(post(client, spec_body, 'inflight'))
            try:
                for label in ['prefill', 'transfer', 'frame:0']:
                    (await peer.at(label)).set()
                assert (await task).status_code == 200
                snap = await state(client)
                assert snap['requests']['inflight']['outcome'] == 'error'
                assert snap['quarantined_paths'].get('/'.join(ROUTE)) == 111640
                assert snap['speculation']['inflight']['scratch_pages'] == 0
                assert all(n['slots'] == n['pages'] == n['work_us'] == 0 for n in snap['nodes'].values())
                calls = len(peer.calls)
                assert (await post(client, body, 'quarantine')).status_code == 429
                assert len(peer.calls) == calls
                clock.advance_to(111640)
                assert (await post(client, body, 'expired')).status_code == 200
                assert not (await state(client))['quarantined_paths']
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('reverse', [False, True])
def test_dirty_evidence_visibility_identity_and_eligibility_are_distinct(tmp_path, reverse):
    source = Path(shutil.copytree(SOURCE, tmp_path / 'input'))
    # These rows have no valid service qualification. Identity reconciliation
    # nevertheless occurs first; future copies cannot invalidate today's data.
    rows = [
        dict(resource='p3', sample_id='probe-bad-duration', attempt=0, ingested_us=90000, duration='NaN'),
        dict(resource='p3', sample_id='probe-bad-duration', attempt=0, ingested_us=91000, duration='NaN'),
        dict(resource='p3', sample_id='probe-bad-duration', attempt=0, ingested_us=190000, duration=10),
        dict(resource='p3', sample_id='probe-bad-id', attempt=True, ingested_us=90000, duration=10),
        dict(resource='p3', sample_id='probe-conflict', attempt=0, ingested_us=90000, duration=10),
        dict(resource='p3', sample_id='probe-conflict', attempt=0, ingested_us=90000, duration=20),
        dict(resource='p3', sample_id='probe-bad-time', attempt=0, ingested_us='NaN', duration=10),
    ]
    expected = ['excluded', 'duplicate', 'deferred', 'invalid', 'conflict', 'conflict', 'invalid']
    # Reverse independent records while preserving the canonical duplicate's
    # source ordering. The expected disposition is attached to evidence identity.
    if reverse:
        rows[2:], expected[2:] = rows[2:][::-1], expected[2:][::-1]
    raw = source / 'phase/lifecycle-probe.jsonl'
    raw.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    manifest = read(source / 'phase-exports.json')
    manifest['measurements'].append('phase/lifecycle-probe.jsonl')
    write(source / 'phase-exports.json', manifest)
    compile_fabric(source, tmp_path / 'build', 104100)
    ledger = read(tmp_path / 'build/phase-ledger.json')
    actual = {r['source']: r['disposition'] for r in ledger['rows']}
    for index, disposition in enumerate(expected, 1):
        assert actual[f'phase/lifecycle-probe.jsonl#{index}'] == disposition, f'evidence row {index}: identity, visibility and qualification are different stages'
    assert not any(s['sample_id'].startswith('probe-') for s in ledger['samples'])
    audit = read(tmp_path / 'build/phase-audit.json')
    assert audit['dispositions'] == dict(Counter(r['disposition'] for r in ledger['rows']))
    assert audit['qualified_samples'] == len(ledger['samples'])

"""Audit regressions for public contract edges, independent of private helpers."""
import asyncio
import copy
import json
import shutil

import pytest
from serving_lab.fabric import FabricGateway, compile_fabric
from vllm_router.deadline import ManualClock
from test_serving_lifecycle import BarrierPeer, SOURCE, ROUTE, client_for, post, read, setup_case, state, write


@pytest.mark.parametrize('field', ['kv_schema', 'session_id', 'decode_mode'])
def test_explicit_null_selector_is_rejected_before_dispatch(tmp_path, field):
    source, profile, config, deployments, body = setup_case(tmp_path)
    body[field] = None

    async def run():
        peer = BarrierPeer(config, deployments)
        gateway = FabricGateway(source, profile, clock=ManualClock(105000), transport=peer)
        async with client_for(gateway) as client:
            response = await post(client, body, 'invalid')
            assert response.status_code == 400, f'explicit null {field} is not an omitted field'
            assert not peer.calls
    asyncio.run(run())


class UngatedPeer(BarrierPeer):
    async def pause(self, rid, label):
        pass


def output(response):
    payloads = ['\n'.join(line[5:].lstrip(' ') for line in e.split('\n') if line.startswith('data:'))
                for e in response.text.replace('\r\n', '\n').split('\n\n')]
    events = [json.loads(data) for data in payloads if data and data != '[DONE]']
    return dict(text=''.join(c.get('text', '') for e in events for c in e.get('choices', [])),
                token_ids=[t for e in events for c in e.get('choices', []) for t in c.get('token_ids', [])], done='[DONE]' in payloads)


@pytest.mark.parametrize('chunk', [1, 13, 100000])
@pytest.mark.parametrize('tail', ['bad-event', 'comment', 'blank'])
def test_coalesced_wire_preserves_already_committed_output(tmp_path, chunk, tail):
    source, profile, config, deployments, _ = setup_case(tmp_path)
    row = next(r for r in read(source/'workload.json')['requests'] if r['id'] == ('q11' if tail == 'bad-event' else 'q05'))
    body = dict(row['body'], deadline_us=200000)
    # Network coalescing may combine commit, DONE and the following event.
    wire = ''.join(f['wire'] for f in row['decode_frames']).replace(f'"request_id": "{row["id"]}"', '"request_id": "inflight"')
    if tail == 'comment':
        wire += ': harmless EOF comment'
    if tail == 'blank':
        wire += ' \t\n\n'

    async def run():
        peer = UngatedPeer(config, deployments, frames=[wire], chunk=chunk)
        gateway = FabricGateway(source, profile, clock=ManualClock(row['at_us']), transport=peer)
        async with client_for(gateway) as client:
            response = await post(client, body, 'inflight')
            assert response.status_code == 200
            expected = dict(text='BA', token_ids=[1, 0], done=True) if tail == 'bad-event' else dict(text='甲', token_ids=[2, 3], done=True)
            assert output(response) == expected, 'a later event must not erase earlier commits when bytes coalesce'
            snap = await state(client)
            assert snap['requests']['inflight']['outcome'] == ('error' if tail == 'bad-event' else 'success')
            assert snap['ttft_count'] == 1
            assert all(n['slots'] == n['pages'] == n['work_us'] == 0 for n in snap['nodes'].values())
    asyncio.run(run())


def test_finite_small_temperature_keeps_softmax_stable(tmp_path):
    source, profile, config, deployments, _ = setup_case(tmp_path)
    row = next(r for r in read(source/'workload.json')['requests'] if r['id']=='q04')
    body = dict(row['body'], temperature=1e-308, deadline_us=200000)
    wire = ''.join(f['wire'] for f in row['decode_frames']).replace('"request_id": "q04"','"request_id": "inflight"')

    async def run():
        peer = UngatedPeer(config, deployments, frames=[wire], chunk=100000)
        gateway = FabricGateway(source, profile, clock=ManualClock(row['at_us']), transport=peer)
        async with client_for(gateway) as client:
            response = await post(client, body, 'inflight')
            assert output(response)==dict(text='BB',token_ids=[1,1],done=True)
            assert (await state(client))['requests']['inflight']['outcome']=='success'
    asyncio.run(run())


@pytest.mark.parametrize('target', ['deployment', 'assets'])
def test_nested_deployment_audit_does_not_change_semantic_epoch(tmp_path, target):
    source, profile, config, deployments, body = setup_case(tmp_path)
    replacement = tmp_path/'replacement'
    shutil.copytree(profile, replacement)
    document = read(replacement/'deployment-profile.json')
    if target == 'deployment':
        for row in document['resources'].values():
            row['audit_origin'] = 'permitted extra information'
    write(replacement/'deployment-profile.json', document)

    async def run():
        peer = UngatedPeer(config, deployments)
        gateway = FabricGateway(source, profile, clock=ManualClock(105000), transport=peer)
        async with client_for(gateway) as client:
            before = await state(client)
            if target == 'assets':
                catalogue = read(source/config['decode_assets'])
                for item in catalogue.values():
                    item['audit_origin'] = 'permitted extra information'
                    for shard in item['shards']:
                        shard['audit_rank_note'] = 'does not affect quantization'
                write(source/config['decode_assets'], catalogue)
            await gateway.reload(replacement)
            after = await state(client)
            for resource in config['resources']:
                for field in ['epoch', 'factor', 'work_us', 'slots', 'pages']:
                    assert after['nodes'][resource][field] == before['nodes'][resource][field], f'additional audit changed {resource}/{field}'
            assert (await post(client, body, 'after-reload')).status_code == 200
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('sample_id', None), ('sample_id', ''), ('resource', None)])
def test_malformed_measurement_identity_is_invalid_not_excluded(tmp_path, field, value):
    source = tmp_path/'input'
    shutil.copytree(SOURCE, source)
    row = dict(resource='p3', sample_id='audit-identity', attempt=0, ingested_us=90000, duration=10)
    row[field] = value
    (source/'phase/identity-probe.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
    manifest = read(source/'phase-exports.json')
    manifest['measurements'].append('phase/identity-probe.jsonl')
    write(source/'phase-exports.json', manifest)
    compile_fabric(source, tmp_path/'build', 104100)
    states = {r['source']:r['disposition'] for r in read(tmp_path/'build/phase-ledger.json')['rows']}
    assert states['phase/identity-probe.jsonl#1'] == 'invalid'

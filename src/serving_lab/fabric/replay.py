"""Investigative phase-aware CPU replay over the real completions ASGI entry."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
from vllm_router.deadline import ManualClock
from serving_lab.storage import read_json, write_json
from .compile import compile_fabric
from .runtime import FabricGateway
from .wire_fixture import Backend as WireBackend


async def settle(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


def events(work):
    output = []
    for rank,field in [(0,'cancel'),(1,'end'),(2,'first'),(3,'transfer'),(4,'prefill')]:
        output.extend((r[field+'_us'],rank,r['id'],field,r) for r in work['requests'] if r.get(field+'_us') is not None)
    for rank,field in [(5,'reloads'),(6,'deployments'),(7,'observations'),(8,'caches'),(9,'topology')]:
        output.extend((r['at_us'],rank,str(i),field,r) for i,r in enumerate(work[field]))
    output.extend((r['at_us'],9,r['id'],'arrival',r) for r in work['requests'])
    output.extend((t,10,str(i),'checkpoint',{}) for i,t in enumerate(work['checkpoints']))
    return sorted(output,key=lambda e:e[:3])


async def replay(source, workload, output):
    source, output = Path(source), Path(output)
    work = read_json(workload)
    dirs = {}
    for row in work['builds']:
        dirs[row['name']] = output/'profiles'/row['name']
        compile_fabric(source,dirs[row['name']],row['as_of_us'])
    backend = WireBackend(work['requests'],read_json(source/'fabric.json'))
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
                elif kind in ['prefill','transfer','first','end'] and rid in tasks and not tasks[rid].done():
                    backend.gates[rid,kind].set()
                    next_phase = dict(prefill='transfer',transfer='first',first='end').get(kind)
                    await settle(lambda:tasks[rid].done() or (next_phase is not None and (rid,next_phase) in backend.waiting))
                elif kind == 'cancel' and rid in tasks:
                    tasks[rid].cancel()
                    await asyncio.gather(tasks[rid],return_exceptions=True)
                elif kind == 'reloads':
                    await gateway.reload(dirs[row['build']])
                elif kind == 'deployments':
                    operation = row.get('operation') or row.get('op')
                    if operation == 'prepare':
                        await gateway.prepare_reload(dirs[row['build']])
                    elif operation == 'commit':
                        manifest = read_json(dirs[row['build']] / 'bundle-manifest.json')
                        await gateway.commit_reload(manifest['bundle_id'], row.get('parent_bundle'))
                    elif operation == 'abort':
                        bundle_id = None
                        if row.get('build') in dirs:
                            bundle_id = read_json(dirs[row['build']] / 'bundle-manifest.json')['bundle_id']
                        await gateway.abort_reload(bundle_id)
                    else:
                        gateway.journal.audit('reload', 'invalid_operation')
                elif kind == 'observations':
                    gateway.observe(row['rows'])
                elif kind == 'caches':
                    gateway.cache(row)
                elif kind == 'topology':
                    gateway.update_topology(row.get('updates', []), row.get('topology_epoch'))
                elif kind == 'checkpoint':
                    checkpoints.append(dict(at_us=at,**(await client.get('/fabric/diagnostics')).json(),dispatch=copy_calls(backend.calls)))
            replies = await asyncio.gather(*tasks.values(),return_exceptions=True)
            result = dict(checkpoints=checkpoints,statuses={rid:r.status_code if isinstance(r,httpx.Response) else 'cancelled' for rid,r in zip(tasks,replies)})
            write_json(output/'fabric-evaluation.json',result)
            output.mkdir(exist_ok=True,parents=True)
            (output/'metrics.prom').write_text((await client.get('/metrics')).text,encoding='utf-8')
            return result
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(),return_exceptions=True)


def copy_calls(calls):
    return sorted(calls,key=lambda row:(row['id'],row['url']))

"""Continuous arrivals over the upstream ASGI forwarding path, without GPUs."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
from vllm_router.deadline import ManualClock
from .build import build_profiles
from .runtime import ServingGateway
from .storage import read_json, write_json


class StreamBackend:
    def __init__(self, requests):
        self.requests = {r['id']: r for r in requests}
        self.first = {rid: asyncio.Event() for rid in self.requests}
        self.end = {rid: asyncio.Event() for rid in self.requests}
        self.ready, self.tokens, self.sent = set(), set(), []

    async def body(self, rid):
        yield b': heartbeat\n\ndata: {"choices":[{"text":""}]}\n\n'
        self.ready.add(rid)
        await self.first[rid].wait()
        wire = 'data: {"choices":[{"text":"甲"}]}\n\n'.encode()
        yield wire[:30]
        yield wire[30:]
        self.tokens.add(rid)
        await self.end[rid].wait()
        yield b'data: [DONE]\n\n'

    @asynccontextmanager
    async def request(self, **kwargs):
        rid = kwargs['headers']['X-Request-Id']
        row = self.requests[rid]
        self.sent.append([rid, kwargs['url'].split('/')[2].split(':')[0]])
        if row.get('connect_error'):
            self.ready.add(rid)
            raise OSError('Controlled backend connection failure')
        timing = row['timing']
        headers = {'content-type':'text/event-stream', 'x-service-sample':timing['sample'],
                   'x-service-us':str(timing['service_us']), 'x-baseline-us':str(timing['baseline_us'])}
        yield SimpleNamespace(status=200, headers=headers, content=SimpleNamespace(iter_any=lambda: self.body(rid)))


async def settled(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


def timeline(workload):
    events = []
    for kind, rank, field in [('reload',3,'reloads'), ('observe',4,'observations'), ('cache',5,'caches')]:
        events.extend((r['at_us'],rank,i,kind,r) for i,r in enumerate(workload[field]))
    for i, row in enumerate(sorted(workload['requests'], key=lambda r:r['id'])):
        events.append((row['at_us'],6,i,'arrival',row))
        for kind, rank, field in [('cancel',0,'cancel_us'), ('end',1,'end_us'), ('first',2,'first_us')]:
            if row.get(field) is not None:
                events.append((row[field],rank,i,kind,row))
    events.extend((t,7,i,'checkpoint',{}) for i,t in enumerate(workload['checkpoints']))
    return sorted(events, key=lambda e:e[:3])


async def replay_workload(input_dir, workload_path, output_dir):
    workload = read_json(workload_path)
    output = Path(output_dir)
    directories = {}
    for build in workload['builds']:
        directory = output / 'profiles' / build['name']
        build_profiles(input_dir, directory, build['as_of_us'])
        directories[build['name']] = directory
    clock = ManualClock(workload['window'][0])
    backend = StreamBackend(workload['requests'])
    gateway = ServingGateway(input_dir, directories[workload['initial']], clock=clock, transport=backend)
    tasks, checkpoints = {}, []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app, raise_app_exceptions=False), base_url='http://gateway') as client:
        try:
            for at, _, _, kind, row in timeline(workload):
                clock.advance_to(at)
                rid = row.get('id')
                if kind == 'arrival':
                    tasks[rid] = asyncio.create_task(client.post('/v1/completions', json=row['body'], headers={'X-Request-Id':rid}))
                    if not any(r['at_us']==at and r['id']>rid for r in workload['requests']):
                        await settled(lambda: all(task.done() or request_id in backend.ready for request_id,task in tasks.items()))
                    if row.get('connect_error'):
                        await settled(tasks[rid].done)
                elif kind == 'cancel' and rid in tasks:
                    tasks[rid].cancel()
                    await asyncio.gather(tasks[rid], return_exceptions=True)
                elif kind in ('first','end') and rid in tasks and not tasks[rid].done():
                    (backend.first if kind == 'first' else backend.end)[rid].set()
                    await settled(lambda: tasks[rid].done() or (kind == 'first' and rid in backend.tokens))
                elif kind == 'reload':
                    await gateway.reload(directories[row['build']])
                elif kind == 'observe':
                    gateway.observe(row['rows'])
                elif kind == 'cache':
                    gateway.cache(row['claims'], row['available'])
                elif kind == 'checkpoint':
                    inspection = (await client.get('/deadline/diagnostics')).json()
                    checkpoints.append(dict(at_us=at, **inspection, dispatch=sorted(backend.sent)))
            replies = await asyncio.gather(*tasks.values(), return_exceptions=True)
            statuses = {rid:r.status_code if isinstance(r,httpx.Response) else 'cancelled' for rid,r in zip(tasks,replies)}
            report = dict(checkpoints=checkpoints, statuses=statuses, summary=gateway.summary(*workload['window']))
            write_json(output / 'routing-evaluation.json', report)
            (output / 'metrics.prom').write_text((await client.get('/metrics')).text, encoding='utf-8')
            return report
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
from vllm_router.deadline import DeadlineGateway, ManualClock
from vllm_router.log import set_log_level


class ReplayTransport:
    """A response transport with explicit phase barriers and backend spans.

    Timestamps are injected protocol observations, not measured GPU latency.
    No assertion, grade or expected routing answer is implemented here.
    """
    def __init__(self, requests):
        self.requests = {r['id']: r for r in requests}
        self.ready, self.sent_token = set(), set()
        self.received = []
        self.first, self.finish = asyncio.Event(), asyncio.Event()

    async def stream(self, rid):
        yield b': ping\n\ndata: {"choices":[{"text":""}]}\n\n'
        self.ready.add(rid)
        await self.first.wait()
        wire = 'data: {"choices":[{"text":"甲"}]}\n\n'.encode()
        for part in (wire[:30], wire[30:]):
            yield part
        self.sent_token.add(rid)
        await self.finish.wait()
        yield b'data: [DONE]\n\n'

    @asynccontextmanager
    async def request(self, **kwargs):
        rid = kwargs['headers']['X-Request-Id']
        item = self.requests[rid]
        self.received.append(dict(request_id=rid, backend=kwargs['url']))
        if item.get('connect_error'):
            self.ready.add(rid)
            raise OSError('simulated connection failure')
        timing = item['timing']
        headers = {'content-type': 'text/event-stream', 'x-service-sample': timing['sample'],
                   'x-baseline-us': str(timing['baseline_us']), 'x-service-us': str(timing['service_us'])}
        yield SimpleNamespace(status=200, headers=headers,
                              content=SimpleNamespace(iter_any=lambda: self.stream(rid)))


async def wait_for(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(.001)


async def replay(scenario):
    clock = ManualClock()
    gateway = DeadlineGateway(scenario['config'], clock=clock, transport=object())
    phases = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app, raise_app_exceptions=False), base_url='http://gateway') as client:
        for block in scenario['rounds']:
            clock.advance_to(block['at_us'])
            gateway.observe(block['snapshots'])
            gateway.cache(block['claims'], block['cache_available'])
            transport = ReplayTransport(block['requests'])
            gateway.app.state.aiohttp_client_wrapper = lambda: transport
            tasks = {r['id']: asyncio.create_task(client.post('/v1/completions', json=r['body'], headers={'X-Request-Id':r['id']})) for r in block['requests']}
            try:
                await wait_for(lambda: all(t.done() or rid in transport.ready for rid, t in tasks.items()))
                before = gateway.inspect()
                for rid in block['cancel']:
                    tasks[rid].cancel()
                await asyncio.gather(*(tasks[rid] for rid in block['cancel']), return_exceptions=True)
                live = {rid for rid in transport.ready if rid not in block['cancel'] and not transport.requests[rid].get('connect_error')}
                clock.advance_to(block['first_us'])
                transport.first.set()
                await wait_for(lambda: live <= transport.sent_token)
                during = gateway.inspect()
                clock.advance_to(block['end_us'])
                transport.finish.set()
                responses = await asyncio.gather(*tasks.values(), return_exceptions=True)
                phases.append(dict(at_us=block['at_us'], before=before, during=during, after=gateway.inspect(),
                                   dispatch=transport.received, statuses=[r.status_code if isinstance(r, httpx.Response) else 'cancelled' for r in responses]))
            finally:
                for task in tasks.values():
                    task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
        metrics = (await client.get('/metrics')).text
    return dict(scenario=scenario['name'], summary=gateway.summary(*scenario['window']), phases=phases), metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    set_log_level('error')
    scenario = json.loads(args.scenario.read_text(encoding='utf-8-sig'))
    report, metrics = asyncio.run(replay(scenario))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'routing-evaluation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'metrics.prom').write_text(metrics, encoding='utf-8')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()

"""Deterministic backend fixture; computes wire signatures independently."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from types import SimpleNamespace


class Backend:
    def __init__(self, rows, config):
        self.rows = {r['id']: r for r in rows}
        self.config = config
        self.calls, self.sent, self.waiting = [], [], set()
        self.gates = {(rid, p): asyncio.Event() for rid in self.rows for p in ['prefill', 'transfer', 'first', 'end']}

    async def body(self, rid):
        row = self.rows[rid]
        mode = row.get('stream_mode', 'split_utf8')
        yield b': heartbeat\n\ndata: {"choices":[{"text":""}]}\n\n'
        self.waiting.add((rid, 'first'))
        await self.gates[rid, 'first'].wait()
        if mode == 'done_first':
            yield b'data: [DONE]\n\n'
        wire = ('data: {"choices":\ndata: [{"text":"甲"}]}\n\n' if mode == 'multiline'
                else 'data: {"choices":[{"text":"甲"}]}\r\n\r\n').encode()
        for byte in wire:
            yield bytes([byte])
        self.waiting.add((rid, 'end'))
        await self.gates[rid, 'end'].wait()
        if row.get('fail_phase') == 'decode':
            raise OSError('decode failed')
        if mode == 'invalid_json':
            yield b'data: {broken json}\n\n'
        if mode == 'invalid_utf8':
            yield b'data: \xff\n\n'
        if mode == 'truncated_utf8':
            yield b'\xe7'
            return
        yield b'data: [DONE]\n' if mode == 'torn_done' else b'data: [DONE]\n\n'
        if mode == 'after_done':
            yield 'data: {"choices":[{"text":"乙"}]}\n\n'.encode()

    @asynccontextmanager
    async def request(self, **kw):
        rid = kw['headers']['X-Request-Id']
        resource = kw['url'].split('/')[2]
        role = self.config['resources'][resource]['role']
        phase = 'transfer' if role == 'link' else role
        call = dict(id=rid, resource=resource, url=kw['url'], body=kw['json'])
        self.calls.append(call)
        self.sent.append(call)
        row, body = self.rows[rid], kw['json']
        timing = row['timing'][phase]
        trace = timing.get('trace_id', f'trace-{rid}')
        phase_id = timing.get('phase_id', f'{rid}:{phase}:v7')
        epoch, service = kw['headers'].get('X-Resource-Epoch', '1'), str(timing['service_us'])
        if row.get('bad_feedback') == 'trace_id': trace = 'trace-other'
        if row.get('bad_feedback') == 'phase_id': phase_id = f'{rid}:wrong:v7'
        if row.get('bad_feedback') == 'epoch': epoch = str(int(epoch)+1)
        material = f"{trace}|{phase_id}|{timing['sample']}|{resource}|{epoch}|{service}"
        headers = {'x-service-sample': timing['sample'], 'x-service-us': service, 'x-baseline-us': '1',
                   'x-trace-id': trace, 'x-phase-id': phase_id, 'x-resource-epoch': epoch,
                   'x-sample-signature': hashlib.sha256(material.encode()).hexdigest()}
        if row.get('bad_feedback') == 'signature': headers['x-sample-signature'] = 'broken'
        if phase == 'decode':
            receipt = dict(request_id=rid, transaction_id=body['transaction_id'], phase=phase,
                           phase_token=body['phase_token'], route_id=body['route_id'],
                           route_generation=str(body['route_generation']), topology_epoch=str(body['topology_epoch']),
                           resource=resource, layout=self.config['resources'][resource]['layout'])
            if row.get('bad_ack') == phase:
                field = row.get('bad_ack_field', 'request_id')
                receipt[field] = '0'+str(receipt[field]) if field.endswith('generation') else 'wrong'
            headers.update({'x-ack-'+k.replace('_', '-'): v for k, v in receipt.items()})
            yield SimpleNamespace(status=200, headers=headers, content=SimpleNamespace(iter_any=lambda: self.body(rid)))
            return
        self.waiting.add((rid, phase))
        await self.gates[rid, phase].wait()
        if row.get('fail_phase') == phase: raise OSError('phase unavailable')
        ack = dict(request_id=rid, resource=resource, layout=self.config['resources'][resource]['layout'],
                   kv_handle=f'{rid}:{resource}', transaction_id=body['transaction_id'], phase=phase,
                   phase_token=body['phase_token'], route_id=body['route_id'],
                   route_generation=body['route_generation'], topology_epoch=body['topology_epoch'])
        if phase == 'prefill': ack['cached_tokens'] = body['cached_tokens']
        if phase == 'transfer': ack['target'] = body['target']
        if row.get('bad_ack') == phase:
            field = row.get('bad_ack_field', 'request_id')
            ack[field] = str(ack[field]) if field in ['route_generation', 'topology_epoch'] else ('' if field == 'kv_handle' else 'wrong')
        async def payload(): return ack
        yield SimpleNamespace(status=200, headers=headers, json=payload)

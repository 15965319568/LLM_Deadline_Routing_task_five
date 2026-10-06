"""Joint PD placement and ownership across actual prefill, transfer and decode IO."""
import asyncio
import copy
import math
from collections import Counter, deque
from contextlib import asynccontextmanager
from pathlib import Path
from statistics import median
from types import SimpleNamespace
import aiohttp
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from vllm_router.routers.main_router import main_router
from vllm_router.deadline.clock import MonotonicClock
from vllm_router.deadline.stream import FirstToken
from serving_lab.storage import read_json
from .compile import interpolate
from .evidence import integer, number, prefix_pages
from .legacy_control import load_control
from .credit import CreditLedger


def load(directory, config, now):
    document = read_json(Path(directory)/'fabric-profiles.json')
    if integer(document['as_of_us']) > now or set(document['resources']) != set(config['resources']):
        raise ValueError('future or incomplete profile set')
    for identity, spec in config['resources'].items():
        row = document['resources'][identity]
        if row['role'] != spec['role'] or row['layout'] != spec['layout'] or row['axis'] != spec['axis']:
            raise ValueError('profile condition mismatch')
        if len(row['service_us']) != len(spec['axis']) or len(row['support']) != len(spec['axis']):
            raise ValueError('profile shape mismatch')
        for cost, support in zip(row['service_us'], row['support']):
            support = integer(support)
            if (cost is None) != (support < config['minimum_samples']):
                raise ValueError('unsupported cell is not unknown')
            if cost is not None:
                number(cost)
    return document['resources']


def semantic(row):
    return {key: row[key] for key in ['role', 'layout', 'axis', 'service_us']}


class FabricGateway:
    def __init__(self, source_dir, profile_dir, clock=None, transport=None):
        self.config = read_json(Path(source_dir)/'fabric.json')
        self.clock = clock or MonotonicClock()
        self.surfaces = load(profile_dir, self.config, self.clock.now_us())
        self.policies = load_control(profile_dir)
        self.config['tenant_limits'] = {t: dict(p['limits']) for t, p in self.policies.items()}
        self.credit = CreditLedger(self.config)
        self.transport = transport
        self.lock = asyncio.Lock()
        self.epochs = {key: 1 for key in self.surfaces}
        self.factors = {key: 1.0 for key in self.surfaces}
        self.ratios = {key: deque(maxlen=self.config['feedback_window']) for key in self.surfaces}
        self.seen_feedback = set()
        self.leases = []
        self.snapshots = {}
        self.requests = {}
        self.decisions = {}
        self.ttfts = []
        self.outcomes = Counter()
        @asynccontextmanager
        async def lifespan(app):
            async with self:
                yield
        self.app = FastAPI(lifespan=lifespan)
        self.app.include_router(main_router)
        self.app.state.router = SimpleNamespace(fabric_enabled=True)
        self.app.state.fabric_enabled = True
        self.app.state.fabric_gateway = self
        @self.app.get('/fabric/diagnostics')
        async def diagnostics():
            return self.inspect()
        @self.app.get('/metrics')
        async def metrics():
            return Response(self.metrics(), media_type='text/plain')

    async def __aenter__(self):
        self.owns_transport = self.transport is None
        if self.owns_transport:
            self.transport = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.owns_transport:
            await self.transport.close()
            self.transport = None

    async def reload(self, profile_dir):
        replacement = load(profile_dir, self.config, self.clock.now_us())
        async with self.lock:
            for identity, row in replacement.items():
                if semantic(row) != semantic(self.surfaces[identity]):
                    self.epochs[identity] += 1
                    self.factors[identity] = 1.0
                    self.ratios[identity].clear()
                    self.seen_feedback = {key for key in self.seen_feedback if key[0] != identity}
            self.surfaces = replacement
            self.policies = load_control(profile_dir)
            self.config['tenant_limits'] = {t: dict(p['limits']) for t, p in self.policies.items()}

    def cache(self, leases):
        self.leases = [(f'online#{index}', copy.deepcopy(row)) for index,row in enumerate(leases)]

    def observe(self, rows):
        now = self.clock.now_us()
        for row in rows:
            try:
                identity = row['resource']
                if identity not in self.surfaces or row['owner'] == self.config['owner']:
                    continue
                epoch, seq = integer(row['boot']), integer(row['seq'])
                event = integer(row['event_us'])
                if event > now or integer(row['ingested_us']) > now:
                    continue
                normalized = dict(row, boot=epoch, seq=seq, event_us=event, slots=integer(row['slots']),
                                  pages=integer(row['pages']), work_us=float(number(row['work_us'])))
                key = (identity, row['owner'])
                old = self.snapshots.get(key)
                if old is None or (epoch,seq) > (old['boot'],old['seq']):
                    self.snapshots[key] = normalized
                elif (epoch,seq) == (old['boot'],old['seq']):
                    if any(normalized[k] != old[k] for k in ['event_us','slots','pages','work_us']):
                        old['conflict'] = True
            except (KeyError, ValueError, TypeError, ArithmeticError):
                continue

    def usage(self):
        nodes = {key: dict(slots=0, pages=0, work_us=0.0) for key in self.surfaces}
        now = self.clock.now_us()
        for (key, _), snapshot in self.snapshots.items():
            if not snapshot.get('conflict') and 0 <= now-snapshot['event_us'] <= self.config['snapshot_ttl_us']:
                for field in ['slots','pages','work_us']:
                    nodes[key][field] += snapshot[field]
        for request in self.requests.values():
            if request['stage'] == 'terminal':
                continue
            for key in request['held']:
                nodes[key]['slots'] += 1
                if self.config['resources'][key]['role'] == 'decode':
                    nodes[key]['pages'] += request['pages']
                if not (key == request['path'][2] and request['first_us'] is not None):
                    nodes[key]['work_us'] += request['reserved_us'][key]
        return nodes

    async def admit(self, rid, body):
        async with self.lock:
            if rid in self.decisions:
                return 409, None
            now = self.clock.now_us()
            try:
                if body['model'] != self.config['model'] or body['stream'] is not True or not isinstance(body['prompt'],str):
                    raise ValueError('unsupported body')
                tokens = len(body['prompt'])
                output = integer(body['max_tokens'])
                deadline = integer(body['deadline_us'])
                if output < 1 or deadline < now:
                    raise ValueError('invalid budget')
                tenant = body.get('tenant', self.config.get('default_tenant', 'default'))
                if not isinstance(tenant, str) or not tenant:
                    raise ValueError('invalid tenant')
                limits = self.config.get('tenant_limits', {})
                if limits and tenant not in limits:
                    raise ValueError('unknown tenant')
            except (KeyError, ValueError, TypeError, ArithmeticError):
                self.decisions[rid] = dict(status=400)
                return 400, None
            nodes = self.usage()
            pages = math.ceil((tokens+output)/self.config['page_tokens'])
            tenant_active = [request for request in self.requests.values()
                             if request['stage'] != 'terminal' and request.get('tenant') == tenant]
            tenant_slots = len(tenant_active)
            tenant_pages = sum(request['pages'] for request in tenant_active)
            active_work = sum(request['charged_work_us'] for request in tenant_active)
            credit = self.credit.state(tenant, now)
            choices, supported = [], False
            for path in self.config['paths']:
                p, link, d = path
                specs = [self.config['resources'][key] for key in path]
                if [s['role'] for s in specs] != ['prefill','link','decode'] or len({s['layout'] for s in specs}) != 1:
                    continue
                if specs[1]['source'] != p or specs[1]['target'] != d:
                    continue
                cached = prefix_pages(self.leases,p,body['prompt'],specs[0]['layout'],self.config['page_tokens'],now)
                coordinates = [tokens-cached,tokens*self.config['bytes_per_token'],tokens]
                baseline = {key:interpolate(self.surfaces[key], x) for key,x in zip(path,coordinates)}
                if any(cost is None for cost in baseline.values()):
                    continue
                supported = True
                quota = self.config.get('tenant_limits', {}).get(tenant)
                if quota and (tenant_slots + 1 > integer(quota['max_slots']) or
                              tenant_pages + pages > integer(quota['max_pages'])):
                    continue
                if any(nodes[key]['slots']+1 > spec['slots'] for key,spec in zip(path,specs)) or nodes[d]['pages']+pages > specs[2]['pages']:
                    continue
                reserved = {key:baseline[key]*self.factors[key] for key in path}
                overage = self.credit.required(tenant, active_work, sum(reserved.values()))
                if overage and overage > credit['balance']:
                    continue
                prediction = sum(nodes[key]['work_us']+reserved[key] for key in path)+self.config['safety_us']
                if now+prediction <= deadline:
                    choices.append((prediction,tuple(path),cached,baseline,reserved))
            if not choices:
                status = 429 if supported else 503
                self.decisions[rid] = dict(status=status)
                return status, None
            prediction, path, cached, baseline, reserved = min(choices,key=lambda c:(c[0],c[1]))
            request = dict(path=list(path),cached_tokens=cached,baseline_us=baseline,reserved_us=reserved,
                           epochs={key:self.epochs[key] for key in path},pages=pages,held=set(path),stage='prefill',
                           arrival_us=now,first_us=None,deadline_us=deadline,outcome=None,tenant=tenant)
            request.update(charged_work_us=sum(reserved.values()), policy_revision=self.policies[tenant]['revision'],
                           burst_reserved_us=self.credit.required(tenant, active_work, sum(reserved.values())))
            self.credit.reserve(tenant, request['burst_reserved_us'], now)
            self.requests[rid] = request
            self.decisions[rid] = dict(status=200,path=list(path),cached_tokens=cached,predicted_us=round(prediction,6))
            return 200, request

    def feedback(self, rid, identity, headers):
        row = self.requests[rid]
        try:
            sample = headers['x-service-sample']
            actual = float(number(headers['x-service-us']))
            key = (identity, sample)
            baseline = row['baseline_us'][identity]
            if not sample or key in self.seen_feedback or row['epochs'][identity] != self.epochs[identity] or baseline <= 0:
                return
            self.seen_feedback.add(key)
            self.ratios[identity].append(actual/baseline)
            self.factors[identity] = max(1.0,min(4.0,float(median(self.ratios[identity]))))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return

    def transition(self, rid, phase, headers):
        row = self.requests[rid]
        if row['stage'] == 'terminal':
            return
        identity = row['path'][0 if phase == 'prefill' else 1]
        self.feedback(rid,identity,headers)
        row['held'].discard(identity)
        row['stage'] = 'transfer' if phase == 'prefill' else 'decode'

    def first(self, rid):
        row = self.requests[rid]
        if row['first_us'] is None and row['stage'] != 'terminal':
            row['first_us'] = self.clock.now_us()
            self.ttfts.append(row['first_us']-row['arrival_us'])

    def finish(self, rid, outcome, headers=None):
        row = self.requests[rid]
        if row['stage'] == 'terminal':
            return
        if outcome == 'success' and row['first_us'] is not None:
            self.feedback(rid,row['path'][2],headers or {})
        row['stage'], row['outcome'] = 'terminal', outcome
        row['held'].clear()
        self.credit.settle(row, outcome, self.clock.now_us())
        self.outcomes[outcome] += 1

    async def forward(self, request, endpoint):
        rid = request.headers.get('X-Request-Id')
        if not rid or endpoint != '/v1/completions':
            return JSONResponse(dict(error='fabric requires completions and request id'),status_code=400)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse(dict(error='invalid JSON'),status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(dict(error='invalid JSON body'),status_code=400)
        status, row = await self.admit(rid,body)
        if status != 200:
            return JSONResponse(dict(error='admission rejected'),status_code=status)
        p, link, d = row['path']
        headers = {'X-Request-Id':rid}
        try:
            async with self.transport.request(method='POST',url=self.config['resources'][p]['url']+'/v1/prefill',headers=headers,json=dict(body,cached_tokens=row['cached_tokens'])) as response:
                ack = await response.json()
                if response.status != 200 or ack.get('request_id') != rid or ack.get('resource') != p or ack.get('layout') != self.config['resources'][p]['layout'] or not ack.get('kv_handle'):
                    raise ValueError('prefill acknowledgement mismatch')
                handle = ack['kv_handle']
                self.transition(rid,'prefill',response.headers)
            payload = dict(request_id=rid,source=p,target=d,kv_handle=handle,bytes=len(body['prompt'])*self.config['bytes_per_token'])
            async with self.transport.request(method='POST',url=self.config['resources'][link]['url']+'/v1/kv-transfer',headers=headers,json=payload) as response:
                ack = await response.json()
                if response.status != 200 or ack.get('request_id') != rid or ack.get('resource') != link or ack.get('target') != d or ack.get('layout') != self.config['resources'][d]['layout'] or not ack.get('kv_handle'):
                    raise ValueError('transfer acknowledgement mismatch')
                handle = ack['kv_handle']
                self.transition(rid,'transfer',response.headers)
        except asyncio.CancelledError:
            self.finish(rid,'cancelled')
            raise
        except Exception:
            self.finish(rid,'error')
            return JSONResponse(dict(error='phase failed'),status_code=502)
        async def stream():
            outcome = 'error'
            response_headers = {}
            parser = FirstToken()
            try:
                async with self.transport.request(method='POST',url=self.config['resources'][d]['url']+'/v1/completions',headers=headers,json=dict(body,kv_handle=handle)) as response:
                    response_headers = response.headers
                    if response.status != 200:
                        raise ValueError('decode failed')
                    async for chunk in response.content.iter_any():
                        if parser.feed(chunk):
                            self.first(rid)
                        yield chunk
                    outcome = 'success' if parser.found else 'error'
            except asyncio.CancelledError:
                outcome = 'cancelled'
                raise
            finally:
                self.finish(rid,outcome,response_headers)
        return StreamingResponse(stream(),media_type='text/event-stream',headers=headers)

    def inspect(self):
        nodes = self.usage()
        for key,row in nodes.items():
            row.update(epoch=self.epochs[key],factor=round(self.factors[key],6))
            row['work_us'] = round(row['work_us'],6)
        requests = {rid:dict(stage=row['stage'],outcome=row['outcome'],held=sorted(row['held']),pages=row['pages'],
                            baseline_us={k:round(v,6) for k,v in row['baseline_us'].items()},
                            reserved_us={k:round(v,6) for k,v in row['reserved_us'].items()},epochs=row['epochs'],first_us=row['first_us'])
                    for rid,row in sorted(self.requests.items())}
        return dict(nodes=nodes,decisions=self.decisions,requests=requests,
                    tenant_policies=self.policies,tenant_ledgers=self.credit.rows,
                    ttft_count=len(self.ttfts),ttft_sum_us=sum(self.ttfts),outcomes=dict(self.outcomes))

    def metrics(self):
        lines = ['# TYPE fabric_ttft_seconds summary',f'fabric_ttft_seconds_count {len(self.ttfts)}',f'fabric_ttft_seconds_sum {sum(self.ttfts)/1e6}']
        for key,row in self.inspect()['nodes'].items():
            for field in ['slots','pages','work_us','factor','epoch']:
                lines.append(f'fabric_{field}{{resource="{key}"}} {row[field]}')
        for outcome,count in sorted(self.outcomes.items()):
            lines.append(f'fabric_terminal_total{{outcome="{outcome}"}} {count}')
        return '\n'.join(lines)+'\n'

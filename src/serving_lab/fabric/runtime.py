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
from .policy import StreamTracker, priority_factor, record_start, request_fields, start_allowed
from .deployment import load_deployment
from .speculation import assets, sampling, SpeculativeDecoder
from .constraints import load_grammars, DecodeState
from .paged_attention import empty_report


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
        self.deployments = load_deployment(profile_dir, self.config, self.clock.now_us())
        self.source_dir = source_dir
        self.decode_assets = assets(source_dir, self.config, self.deployments)
        self.grammars = load_grammars(profile_dir, self.config, self.clock.now_us())
        self.transport = transport
        self.lock = asyncio.Lock()
        self.epochs = {key: 1 for key in self.surfaces}
        self.factors = {key: 1.0 for key in self.surfaces}
        self.ratios = {key: deque(maxlen=self.config['feedback_window']) for key in self.surfaces}
        self.seen_feedback = set()
        self.leases = []
        self.snapshots = {}
        self.requests = {}
        self.sessions = {}
        self.start_log = {}
        self.decisions = {}
        self.ttfts = []
        self.outcomes = Counter()
        # A backend fault quarantines the complete PD route for a short,
        # deterministic interval. The route is the unit of failure because
        # a partial path can leave incompatible KV state behind.
        self.quarantine = {}
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
        deployments = load_deployment(profile_dir, self.config, self.clock.now_us())
        decode_assets = assets(self.source_dir, self.config, deployments)
        grammars = load_grammars(profile_dir, self.config, self.clock.now_us())
        def relevant(resource, catalogue, deployment):
            return {key:value for key,value in catalogue.items() if self.config['resources'][resource]['role']=='decode' and value['tokenizer_revision']==deployment[resource]['tokenizer_revision']}
        async with self.lock:
            for identity, row in replacement.items():
                if semantic(row) != semantic(self.surfaces[identity]) or deployments[identity] != self.deployments[identity] or decode_assets.get(identity) != self.decode_assets.get(identity) or relevant(identity,grammars,deployments) != relevant(identity,self.grammars,self.deployments):
                    self.epochs[identity] += 1
                    self.factors[identity] = 1.0
                    self.ratios[identity].clear()
                    self.seen_feedback = {key for key in self.seen_feedback if key[0] != identity}
            self.surfaces = replacement
            self.deployments = deployments
            self.decode_assets = decode_assets
            self.grammars = grammars

    def _route_available(self, path, now):
        key = tuple(path)
        until = self.quarantine.get(key, 0)
        if until and until <= now:
            self.quarantine.pop(key, None)
            return True
        return not until or until <= now

    def _fail_phase(self, rid, phase):
        row = self.requests[rid]
        if row['stage'] == 'terminal':
            return
        cooldown = integer(self.config.get('route_quarantine_us', 0))
        if cooldown > 0:
            key = tuple(row['path'])
            self.quarantine[key] = max(self.quarantine.get(key, 0), self.clock.now_us() + cooldown)
        self.finish(rid, 'error')

    def cache(self, leases):
        # Keep the producer lease identity inside the row while assigning a source for reconciliation.
        self.leases = [(f'online#{index}', copy.deepcopy(row)) for index, row in enumerate(leases)]

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
                healthy = row.get('healthy', True)
                draining = row.get('draining', False)
                capacity_epoch = integer(row.get('capacity_epoch', 0))
                if not isinstance(healthy, bool) or not isinstance(draining, bool) or capacity_epoch < 0:
                    continue
                normalized = dict(row, boot=epoch, seq=seq, event_us=event, slots=integer(row['slots']),
                                  pages=integer(row['pages']), work_us=float(number(row['work_us'])),
                                  healthy=healthy, draining=draining, capacity_epoch=capacity_epoch)
                key = (identity, row['owner'])
                old = self.snapshots.get(key)
                if old is not None and epoch == old['boot'] and capacity_epoch < old.get('capacity_epoch', 0):
                    continue
                if old is None or (epoch,seq) > (old['boot'],old['seq']):
                    self.snapshots[key] = normalized
                elif (epoch,seq) == (old['boot'],old['seq']):
                    if any(normalized[k] != old[k] for k in ['event_us','slots','pages','work_us','healthy','draining','capacity_epoch']):
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
                    nodes[key]['pages'] += request['pages'] + (request.get('scratch_pages', 0)+request.get('attention_pages',0))
                if not (key == request['path'][2] and request['first_us'] is not None):
                    nodes[key]['work_us'] += request['reserved_us'][key]
        return nodes

    async def admit(self, rid, body):
        async with self.lock:
            if rid in self.decisions:
                return 409, None
            now = self.clock.now_us()
            try:
                prompt, output, deadline, tenant, priority, session, compatibility = request_fields(body, self.config, now)
                tokens = len(prompt)
                factor = priority_factor(self.config, priority)
                options = sampling(body)
            except (KeyError, ValueError, TypeError, ArithmeticError):
                self.decisions[rid] = dict(status=400)
                return 400, None
            nodes = self.usage()
            pages = math.ceil((tokens+output)/self.config['page_tokens'])
            tenant_active = [request for request in self.requests.values()
                             if request['stage'] != 'terminal' and request.get('tenant') == tenant]
            tenant_slots = len(tenant_active)
            tenant_pages = sum(request['pages'] + request.get('scratch_pages', 0) for request in tenant_active)
            tenant_work = sum(request['charged_work_us'] for request in tenant_active)
            if not start_allowed(self.start_log, tenant, now, self.config):
                self.decisions[rid] = dict(status=429)
                return 429, None
            sticky = self.sessions.get(session) if session else None
            if sticky and sticky[1] <= now:
                self.sessions.pop(session, None)
                sticky = None
            choices, supported = [], False
            for path in self.config['paths']:
                p, link, d = path
                if options is not None and d not in self.decode_assets:
                    continue
                if options is not None and options.get('decode_kernel')=='paged-gqa' and 'paged_attention' not in self.decode_assets[d]:
                    continue
                if options is not None and options.get('grammar') is not None:
                    rule = self.grammars.get(options['grammar'])
                    if rule is None or rule['tokenizer_revision'] != self.deployments[d]['tokenizer_revision']:
                        continue
                specs = [self.config['resources'][key] for key in path]
                if [s['role'] for s in specs] != ['prefill','link','decode'] or len({s['layout'] for s in specs}) != 1:
                    continue
                if specs[1]['source'] != p or specs[1]['target'] != d:
                    continue
                if sticky and tuple(path) != tuple(sticky[0]):
                    continue
                if any(any(compatibility[field] is not None and self.deployments[key].get(field) != compatibility[field]
                           for field in compatibility if field != 'explicit') for key in path):
                    continue
                blocked = False
                for key in path:
                    for (resource, _), snapshot in self.snapshots.items():
                        if resource != key:
                            continue
                        age = now - snapshot['event_us']
                        if (not snapshot.get('conflict') and 0 <= age <= self.config['snapshot_ttl_us'] and
                                (not snapshot.get('healthy', True) or snapshot.get('draining', False))):
                            blocked = True
                            break
                    if blocked:
                        break
                if blocked:
                    continue
                cached = prefix_pages(self.leases,p,body['prompt'],specs[0]['layout'],self.config['page_tokens'],now,session_id=session,compatibility=dict(compatibility, **{f:self.deployments[p][f] for f in ('deployment_id','generation','kv_schema')}))
                coordinates = [tokens-cached,tokens*self.config['bytes_per_token'],tokens]
                baseline = {key:interpolate(self.surfaces[key], x) for key,x in zip(path,coordinates)}
                if any(cost is None for cost in baseline.values()):
                    continue
                supported = True
                if not self._route_available(path, now):
                    continue
                quota = self.config.get('tenant_limits', {}).get(tenant)
                if quota and (tenant_slots + 1 > integer(quota['max_slots']) or
                              tenant_pages + pages > integer(quota['max_pages'])):
                    continue
                if any(nodes[key]['slots']+1 > spec['slots'] for key,spec in zip(path,specs)) or nodes[d]['pages']+pages > specs[2]['pages']:
                    continue
                reserved = {key:baseline[key]*self.factors[key] for key in path}
                charged_work = sum(reserved.values()) * factor
                if quota and 'max_work_us' in quota and tenant_work + charged_work > float(number(quota['max_work_us'])):
                    continue
                prediction = sum(nodes[key]['work_us']+reserved[key] for key in path)+self.config['safety_us']
                if now+prediction <= deadline:
                    choices.append((prediction,tuple(path),cached,baseline,reserved,charged_work))
            if not choices:
                status = 429 if supported else 503
                self.decisions[rid] = dict(status=status)
                return status, None
            prediction, path, cached, baseline, reserved, charged_work = min(choices,key=lambda c:(c[0],c[1]))
            if options is not None:
                asset = self.decode_assets[path[2]]
                if any(t >= len(asset['vocabulary']) or t == asset['eos_id'] for t in options['prompt_token_ids']):
                    self.decisions[rid] = dict(status=400)
                    return 400,None
                options = copy.deepcopy(options)
                options['grammar_asset'] = copy.deepcopy(self.grammars.get(options.get('grammar')))
            request = dict(path=list(path),cached_tokens=cached,baseline_us=baseline,reserved_us=reserved,
                           charged_work_us=charged_work,
                           epochs={key:self.epochs[key] for key in path},pages=pages,held=set(path),stage='prefill',
                           arrival_us=now,first_us=None,deadline_us=deadline,outcome=None,tenant=tenant,
                           priority=priority,session_id=session,compatibility=compatibility,
                           deployments={key:copy.deepcopy(self.deployments[key]) for key in path})
            self.requests[rid] = request
            if options is not None:
                request.update(sampling=options, decode_asset=copy.deepcopy(self.decode_assets[path[2]]),
                               scratch_pages=0, speculation=dict(proposed=0,accepted=0,corrected=0,committed=0,windows=0,scratch_pages=0))
                request['decoding'] = DecodeState(request['decode_asset'],options,options['grammar_asset']).report()
                if options.get('decode_kernel')=='paged-gqa':
                    request['attention_pages']=0
                    request['paged_attention']=empty_report(request['decode_asset']['paged_attention']['revision'])
            record_start(self.start_log, tenant, now)
            if session:
                self.sessions[session] = (tuple(path), now + integer(self.config.get('session_affinity_ttl_us', 0)))
            self.decisions[rid] = dict(status=200,path=list(path),cached_tokens=cached,predicted_us=round(prediction,6))
            return 200, request

    def feedback(self, rid, identity, headers):
        row = self.requests[rid]
        try:
            sample = headers['x-service-sample']
            actual = float(number(headers['x-service-us']))
            key = (identity, sample)
            baseline = row['baseline_us'][identity]
            if not sample or key in self.seen_feedback or baseline <= 0:
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
        row['scratch_pages'] = 0
        if 'paged_attention' in row:
            row['attention_pages']=0
            row['paged_attention'].update(resident_pages=0,staged_pages=0)
        if 'speculation' in row:
            row['speculation']['scratch_pages'] = 0
        if 'decoding' in row:
            row['decoding']['pending_bytes'] = 0
        self.outcomes[outcome] += 1

    def reserve_draft(self, rid, length):
        row = self.requests[rid]
        pages = math.ceil(length/self.config['page_tokens'])
        resource = row['path'][2]
        total = sum(r['pages']+(r.get('scratch_pages', 0)+r.get('attention_pages',0)) for r in self.requests.values()
                    if r['stage'] != 'terminal' and r.get('tenant') == row['tenant'])
        quota = self.config.get('tenant_limits', {}).get(row['tenant'])
        if self.usage()[resource]['pages']+pages > self.config['resources'][resource]['pages'] or (quota and total+pages > quota['max_pages']):
            raise ValueError('speculative KV budget exhausted')
        row['scratch_pages'] = pages
        return pages

    def allocate_attention(self, rid, count):
        row=self.requests[rid]; previous=row.get('attention_pages',0)
        if count>previous:
            delta=count-previous; resource=row['path'][2]
            total=sum(r['pages']+r.get('scratch_pages',0)+r.get('attention_pages',0) for r in self.requests.values() if r['stage']!='terminal' and r['tenant']==row['tenant'])
            quota=self.config.get('tenant_limits',{}).get(row['tenant'])
            if self.usage()[resource]['pages']+delta>self.config['resources'][resource]['pages'] or quota and total+delta>quota['max_pages']:
                raise ValueError('paged attention workspace budget exhausted')
        row['attention_pages']=count

    def release_draft(self, rid):
        self.requests[rid]['scratch_pages'] = 0

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
        strict = row['compatibility'].get('explicit', False) or 'sampling' in row
        expected_deployment = row['deployments'][p]
        if strict:
            headers.update({'X-Deployment-Id':expected_deployment['deployment_id'], 'X-Deployment-Generation':str(expected_deployment['generation']), 'X-KV-Schema':expected_deployment['kv_schema']})
        try:
            async with self.transport.request(method='POST',url=self.config['resources'][p]['url']+'/v1/prefill',headers=headers,json=dict(body,cached_tokens=row['cached_tokens'])) as response:
                ack = await response.json()
                if (response.status != 200 or ack.get('request_id') != rid or
                        ack.get('resource') != p or
                        ack.get('layout') != self.config['resources'][p]['layout'] or
                        ack.get('cached_tokens') != row['cached_tokens'] or
                        not isinstance(ack.get('kv_handle'), str) or
                        not ack['kv_handle'].startswith(f'{rid}:{p}:') or
                        not ack['kv_handle'][len(f'{rid}:{p}:'):] or
                        (strict and (ack.get('deployment_id') != expected_deployment['deployment_id'] or
                                     ack.get('generation') != expected_deployment['generation'] or
                                     ack.get('kv_schema') != expected_deployment['kv_schema']))):
                    raise ValueError('prefill acknowledgement mismatch')
                handle = ack['kv_handle']
                self.transition(rid,'prefill',response.headers)
            payload = dict(request_id=rid,source=p,target=d,kv_handle=handle,bytes=len(body['prompt'])*self.config['bytes_per_token'])
            if strict:
                payload.update(deployment_id=expected_deployment['deployment_id'], generation=expected_deployment['generation'], kv_schema=expected_deployment['kv_schema'])
            if strict:
                headers.update({'X-Deployment-Id':row['deployments'][link]['deployment_id'], 'X-Deployment-Generation':str(row['deployments'][link]['generation']), 'X-KV-Schema':row['deployments'][link]['kv_schema']})
            async with self.transport.request(method='POST',url=self.config['resources'][link]['url']+'/v1/kv-transfer',headers=headers,json=payload) as response:
                ack = await response.json()
                if (response.status != 200 or ack.get('request_id') != rid or
                        ack.get('resource') != link or ack.get('target') != d or
                        ack.get('layout') != self.config['resources'][d]['layout'] or
                        not isinstance(ack.get('kv_handle'), str) or
                        not ack['kv_handle'].startswith(f'{rid}:{link}:') or
                        not ack['kv_handle'][len(f'{rid}:{link}:'):] or
                        (strict and (ack.get('deployment_id') != row['deployments'][link]['deployment_id'] or
                                     ack.get('generation') != row['deployments'][link]['generation'] or
                                     ack.get('kv_schema') != row['deployments'][link]['kv_schema']))):
                    raise ValueError('transfer acknowledgement mismatch')
                handle = ack['kv_handle']
                self.transition(rid,'transfer',response.headers)
        except asyncio.CancelledError:
            self.finish(rid,'cancelled')
            raise
        except Exception:
            self._fail_phase(rid, 'prefill/transfer')
            return JSONResponse(dict(error='phase failed'),status_code=502)
        if strict:
            headers.update({'X-Deployment-Id':row['deployments'][d]['deployment_id'], 'X-Deployment-Generation':str(row['deployments'][d]['generation']), 'X-KV-Schema':row['deployments'][d]['kv_schema']})
        async def stream():
            outcome = 'error'
            response_headers = {}
            parser = StreamTracker()
            speculative = None
            if 'sampling' in row:
                decode_asset = self.decode_assets[d] if row['sampling'].get('decode_kernel')=='paged-gqa' else row['decode_asset']
                speculative = SpeculativeDecoder(rid, decode_asset, row['sampling'], body['max_tokens'],
                    lambda n:self.reserve_draft(rid,n), lambda:self.release_draft(rid), lambda n:self.allocate_attention(rid,n))
                speculative.stats = row['speculation']
                speculative.decoding = row['decoding']
                if speculative.paged: speculative.paged.report=row['paged_attention']
                if row['sampling'].get('grammar') is not None:
                    headers.update({'X-Grammar-Id':row['sampling']['grammar'],'X-Grammar-Revision':str(row['sampling']['grammar_asset']['revision'])})
            try:
                async with self.transport.request(method='POST',url=self.config['resources'][d]['url']+'/v1/completions',headers=headers,json=dict(body,kv_handle=handle)) as response:
                    response_headers = response.headers
                    if response.status != 200:
                        raise ValueError('decode failed')
                    async for chunk in response.content.iter_any():
                        outgoing = speculative.feed(chunk) if speculative else [chunk]
                        for output in outgoing:
                            if parser.feed(output):
                                self.first(rid)
                            yield output
                    if speculative:
                        speculative.close()
                    parser.close()
                    outcome = 'success' if (speculative or parser.first) and parser.done and not parser.invalid else 'error'
            except asyncio.CancelledError:
                outcome = 'cancelled'
                raise
            except Exception:
                self._fail_phase(rid, 'decode')
                raise
            finally:
                if speculative and speculative.paged: speculative.paged.cleanup()
                if outcome == 'error':
                    self._fail_phase(rid, 'decode')
                else:
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
        quarantined = {'/'.join(path): until for path, until in sorted(self.quarantine.items())
                       if until > self.clock.now_us()}
        result = dict(nodes=nodes,decisions=self.decisions,requests=requests,
                    ttft_count=len(self.ttfts),ttft_sum_us=sum(self.ttfts),
                    outcomes=dict(self.outcomes),quarantined_paths=quarantined)
        attention = {rid:copy.deepcopy(row['paged_attention']) for rid,row in self.requests.items() if 'paged_attention' in row}
        if attention: result['paged_attention']=attention
        speculation = {rid:copy.deepcopy(row['speculation']) for rid,row in self.requests.items() if 'speculation' in row}
        if speculation:
            result['speculation'] = speculation
            result['decoding'] = {rid:copy.deepcopy(row['decoding']) for rid,row in self.requests.items() if 'decoding' in row}
        return result

    def metrics(self):
        lines = ['# TYPE fabric_ttft_seconds summary',f'fabric_ttft_seconds_count {len(self.ttfts)}',f'fabric_ttft_seconds_sum {sum(self.ttfts)/1e6}']
        for key,row in self.inspect()['nodes'].items():
            for field in ['slots','pages','work_us','factor','epoch']:
                lines.append(f'fabric_{field}{{resource="{key}"}} {row[field]}')
        for outcome,count in sorted(self.outcomes.items()):
            lines.append(f'fabric_terminal_total{{outcome="{outcome}"}} {count}')
        for resource,spec in self.config['resources'].items():
            if spec['role'] == 'decode':
                for field in ['proposed','accepted','corrected','committed','windows','scratch_pages']:
                    total = sum(r.get('speculation', {}).get(field, 0) for r in self.requests.values() if r['path'][2] == resource)
                    lines.append(f'fabric_speculative_{field}{{resource="{resource}"}} {total}')
        return '\n'.join(lines)+'\n'

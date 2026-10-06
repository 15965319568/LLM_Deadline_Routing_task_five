"""First PD pilot: independent placement with colocated capacity compatibility."""
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path
from statistics import median
from types import SimpleNamespace
import aiohttp
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from vllm_router.routers.main_router import main_router
from vllm_router.deadline.clock import MonotonicClock
from serving_lab.storage import read_json
from .catalogue import catalogue
from .evidence import cached_tokens


class FabricGateway:
    def __init__(self, source_dir, profile_dir, clock=None, transport=None):
        self.source=Path(source_dir); self.config=read_json(self.source/'fabric.json')
        self.clock=clock or MonotonicClock(); self.transport=transport
        self.profiles=read_json(Path(profile_dir)/'fabric-profiles.json')
        self.costs=catalogue(self.source,self.config)
        self.requests={}; self.decisions={}; self.leases=[]; self.snapshots={}; self.ttfts=[]; self.outcomes={}
        self.factors={r:1.0 for r in self.costs}; self.epochs={r:1 for r in self.costs}
        @asynccontextmanager
        async def lifespan(app):
            async with self: yield
        self.app=FastAPI(lifespan=lifespan); self.app.include_router(main_router)
        self.app.state.router=SimpleNamespace(fabric_enabled=True)
        self.app.state.fabric_enabled=True; self.app.state.fabric_gateway=self
        @self.app.get('/fabric/diagnostics')
        async def diagnostics(): return self.inspect()
        @self.app.get('/metrics')
        async def metrics(): return Response(self.metrics(),media_type='text/plain')

    async def __aenter__(self):
        self.owns_transport=self.transport is None
        if self.owns_transport: self.transport=aiohttp.ClientSession()
        return self

    async def __aexit__(self,*args):
        if self.owns_transport: await self.transport.close()

    def cache(self,leases): self.leases=copy.deepcopy(leases)

    def observe(self,rows):
        for row in rows: self.snapshots[row['resource']]=dict(row)

    async def reload(self,profile_dir):
        self.profiles=read_json(Path(profile_dir)/'fabric-profiles.json')
        for r,row in self.profiles['resources'].items():
            self.costs[r]=median(row['service_us'])
            self.epochs[r]+=1; self.factors[r]=1.0
        for request in self.requests.values():
            request['reserved_us']={r:self.costs[r] for r in request['path']}

    def usage(self):
        nodes={r:dict(slots=0,pages=0,work_us=0.0,epoch=self.epochs[r],factor=self.factors[r]) for r in self.costs}
        for r,row in self.snapshots.items():
            for field in ['slots','pages','work_us']: nodes[r][field]+=row[field]
        for row in self.requests.values():
            for r in row['held']:
                nodes[r]['slots']+=1; nodes[r]['work_us']+=row['reserved_us'][r]
        return nodes

    async def admit(self,rid,body):
        nodes=self.usage(); specs=self.config['resources']
        # The notebook policy selects each role independently. The transfer
        # broker was originally responsible for adapting endpoint pairs.
        p=min((r for r in specs if specs[r]['role']=='prefill'),key=lambda r:nodes[r]['work_us']+self.costs[r]*self.factors[r])
        d=min((r for r in specs if specs[r]['role']=='decode'),key=lambda r:nodes[r]['slots'])
        link=next(r for r in specs if specs[r]['role']=='link' and specs[r]['source']==p)
        path=[p,link,d]; cached=cached_tokens(self.leases,p)
        predicted=sum(self.costs[r]*self.factors[r] for r in path)
        if self.clock.now_us()+predicted>body['deadline_us'] or nodes[p]['slots']>=specs[p]['slots']:
            self.decisions[rid]=dict(status=429); return 429,None
        row=dict(path=path,cached_tokens=cached,baseline_us={r:self.costs[r] for r in path},reserved_us={r:self.costs[r] for r in path},
                 epochs={r:self.epochs[r] for r in path},held={p},pages=(len(body['prompt'])+3)//4,stage='prefill',outcome=None,first_us=None,arrival_us=self.clock.now_us())
        self.requests[rid]=row; self.decisions[rid]=dict(status=200,path=path,cached_tokens=cached,predicted_us=predicted)
        return 200,row

    def finish(self,rid,outcome,headers=None):
        row=self.requests[rid]; row.update(stage='terminal',outcome=outcome,held=set())
        self.outcomes[outcome]=self.outcomes.get(outcome,0)+1
        if headers and 'x-service-us' in headers:
            ratio=float(headers['x-service-us'])/float(headers.get('x-baseline-us',self.costs[row['path'][2]]))
            for r in row['path']: self.factors[r]=max(1.0,min(4.0,ratio))

    async def forward(self,request,endpoint):
        rid=request.headers.get('X-Request-Id'); body=await request.json(); status,row=await self.admit(rid,body)
        if status!=200: return JSONResponse(dict(error='capacity'),status_code=status)
        p,link,d=row['path']; headers={'X-Request-Id':rid}
        try:
            async with self.transport.request(method='POST',url=self.config['resources'][p]['url']+'/v1/prefill',headers=headers,json=dict(body,cached_tokens=row['cached_tokens'])) as response:
                ack=await response.json(); handle=ack['kv_handle']
                row['held']={link}; row['stage']='transfer'
            payload=dict(request_id=rid,source=p,target=d,kv_handle=handle,bytes=max(0,len(body['prompt'])-row['cached_tokens'])*self.config['bytes_per_token'])
            async with self.transport.request(method='POST',url=self.config['resources'][link]['url']+'/v1/kv-transfer',headers=headers,json=payload) as response:
                ack=await response.json(); handle=ack['kv_handle']
                row['held']={d}; row['stage']='decode'
        except Exception:
            self.finish(rid,'error'); return JSONResponse(dict(error='backend'),status_code=502)
        async def stream():
            async with self.transport.request(method='POST',url=self.config['resources'][d]['url']+'/v1/completions',headers=headers,json=dict(body,kv_handle=handle)) as response:
                async for chunk in response.content.iter_any():
                    if row['first_us'] is None:
                        row['first_us']=self.clock.now_us(); self.ttfts.append(row['first_us']-row['arrival_us']); row['held']=set()
                    yield chunk
                self.finish(rid,'success',response.headers)
        return StreamingResponse(stream(),media_type='text/event-stream')

    def inspect(self):
        rows={rid:{k:(sorted(row[k]) if k=='held' else row[k]) for k in ['stage','outcome','held','pages','baseline_us','reserved_us','epochs','first_us']} for rid,row in self.requests.items()}
        return dict(nodes=self.usage(),decisions=self.decisions,requests=rows,ttft_count=len(self.ttfts),ttft_sum_us=sum(self.ttfts),outcomes=self.outcomes)

    def metrics(self):
        return f'fabric_ttft_seconds_count {len(self.ttfts)}\nfabric_ttft_seconds_sum {sum(self.ttfts)/1e6}\n'

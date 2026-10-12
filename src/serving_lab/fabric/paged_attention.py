"""Versioned int4 KV workspaces and row-parallel grouped-query attention."""
import copy
import math


def uint(x, maximum=None, minimum=0):
    if type(x) is not int or x < minimum or maximum is not None and x > maximum:
        raise ValueError('invalid unsigned integer')
    return x


def real(x, positive=False):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or positive and x <= 0:
        raise ValueError('invalid finite scalar')
    return float(x)


def vector(xs, size, convert):
    if not isinstance(xs, list) or len(xs) != size:
        raise ValueError('tensor dimension mismatch')
    return [convert(x) for x in xs]


def validate_asset(raw, vocabulary):
    dim = uint(raw['head_dim'], 16, 2)
    if dim % 2:
        raise ValueError('RoPE requires an even head dimension')
    kv = uint(raw['kv_heads'], 8, 1)
    heads = raw['head_map']
    if not isinstance(heads, list) or not 1 <= len(heads) <= 16:
        raise ValueError('invalid GQA head map')
    heads = [uint(h, kv-1) for h in heads]
    out = dict(revision=uint(raw['revision'], minimum=1), head_dim=dim, kv_heads=kv, head_map=heads,
               page_size=uint(raw['page_size'], 16, 1), window_size=uint(raw['window_size'], minimum=1),
               sink_tokens=uint(raw['sink_tokens']), rope_theta=real(raw['rope_theta'], True),
               rope_scale=real(raw['rope_scale'], True), nibble_order=raw['nibble_order'])
    if out['rope_theta'] <= 1 or out['nibble_order'] not in ['low-first', 'high-first']:
        raise ValueError('invalid rotary or packed layout')
    out['alibi_slopes'] = vector(raw['alibi_slopes'], len(heads), real)
    ranks = raw['ranks']
    if not isinstance(ranks, list) or not ranks:
        raise ValueError('missing attention ranks')
    out['ranks'] = []
    owned = []
    for part in ranks:
        ids = part['query_heads']
        if not isinstance(ids, list) or not ids:
            raise ValueError('empty rank')
        ids = [uint(h, len(heads)-1) for h in ids]
        owned.extend(ids)
        out['ranks'].append(dict(query_heads=ids, scale=real(part['scale'], True), zero_point=uint(part['zero_point'], 255)))
    if sorted(owned) != list(range(len(heads))):
        raise ValueError('query heads must be partitioned once')
    width = len(heads)*dim
    out['projection'] = vector(raw['projection'], vocabulary, lambda row: vector(row, width, lambda x: uint(x,255)))
    out['projection_scale'] = vector(raw['projection_scale'], vocabulary, lambda x: real(x,True))
    out['projection_zero'] = vector(raw['projection_zero'], vocabulary, lambda x: uint(x,255))
    out['bias'] = vector(raw['bias'], vocabulary, real)
    return out


def empty_report(revision):
    return dict(revision=revision, resident_pages=0, staged_pages=0, committed_windows=0,
                query_rows=0, last_positions=[])


class PageContext:
    def __init__(self, asset, allocate):
        self.asset = asset
        self.spec = asset['paged_attention']
        self.allocate = allocate
        self.cache = {}
        self.stage = None
        self.report = empty_report(self.spec['revision'])

    def begin(self, row, window):
        if self.stage is not None:
            raise ValueError('overlapping paged window')
        ids = {0, *window['tree']} if 'tree' in window else set(range(len(window['proposal'])+1))
        raw = row['query_positions']
        if not isinstance(raw, dict) or set(raw) != {str(i) for i in ids}:
            raise ValueError('query positions must identify every query row')
        positions = {int(k):uint(v) for k,v in raw.items()}
        table = row['kv_table']
        if not isinstance(table, list) or not table:
            raise ValueError('empty logical KV table')
        mapped = []
        for item in table:
            name = item['page_id']
            if not isinstance(name,str) or not name:
                raise ValueError('invalid page identity')
            mapped.append(dict(position=uint(item['position']),page_id=name,
                               version=uint(item['version'],minimum=1),slot=uint(item['slot'],self.spec['page_size']-1)))
        if len({v['position'] for v in mapped}) != len(mapped):
            raise ValueError('duplicate logical position')
        mapped = list({(r['page_id'],r['version'],r['slot']):r for r in mapped}.values())
        self.stage = dict(table=sorted(mapped,key=lambda r:r['position']),positions=positions,
                          tree='tree' in window,pages={},queries={})

    def unpack(self, data):
        size = self.spec['page_size']*self.spec['kv_heads']*self.spec['head_dim']
        if not isinstance(data,str) or len(data) != size:
            raise ValueError('wrong packed KV byte length')
        try:
            packed = bytes.fromhex(data)
        except ValueError:
            raise ValueError('invalid packed KV hex') from None
        if len(packed)*2 != size:
            raise ValueError('wrong packed KV byte length')
        result = []
        for byte in packed:
            pair = [byte & 15, byte >> 4]
            result.extend(pair if self.spec['nibble_order']=='low-first' else pair[::-1])
        return result

    def page(self, row):
        if self.stage is None:
            raise ValueError('page outside window')
        name = row['page_id']
        if not isinstance(name,str) or not name:
            raise ValueError('invalid page identity')
        version = uint(row['version'], minimum=1)
        base = uint(row['base_version'])
        width = self.spec['kv_heads']*self.spec['head_dim']
        if base == 0:
            if 'patches' in row:
                raise ValueError('full page also declares deltas')
            value = {kind:self.unpack(row[kind]) for kind in ['k','v']}
            for kind in ['k','v']:
                value[kind+'_scale'] = vector(row[kind+'_scale'],width,lambda x:real(x,True))
                value[kind+'_zero'] = vector(row[kind+'_zero'],width,lambda x:uint(x,15))
        else:
            if base >= version or (name,base) not in self.cache or any(k in row for k in ['k','v','k_scale','k_zero','v_scale','v_zero']):
                raise ValueError('delta must inherit a committed older page')
            value = dict(self.cache[name,base])
            patches = row['patches']
            if not isinstance(patches,list):
                raise ValueError('invalid delta list')
            seen = {}
            for patch in patches:
                kind = patch['tensor']
                if kind not in ['k','v']:
                    raise ValueError('unknown KV tensor')
                index = uint(patch['index'],len(value[kind])-1)
                quantized = uint(patch['value'],15)
                key = kind,index
                if key in seen and seen[key] != quantized:
                    raise ValueError('conflicting delta entries')
                seen[key] = quantized
                value[kind][index] = quantized
        key = name,version
        previous = self.stage['pages'].get(key,self.cache.get(key))
        if previous is not None and previous != value:
            raise ValueError('conflicting page retransmission')
        if previous is None:
            self.allocate(len(set(self.stage['pages'])|{key}))
        self.stage['pages'][key] = value
        self.report['staged_pages'] = len(set(self.stage['pages'])-set(self.cache))

    def query(self, row):
        if self.stage is None:
            raise ValueError('query outside window')
        rank = uint(row['rank'],len(self.spec['ranks'])-1)
        ids = row['node_ids']
        if not isinstance(ids,list) or any(type(n) is not int for n in ids) or len(ids)!=len(set(ids)) or set(ids)!=set(self.stage['positions']):
            raise ValueError('missing or duplicate query row')
        heads = len(self.spec['ranks'][rank]['query_heads'])
        dim = self.spec['head_dim']
        tensor = lambda rows,n: vector(rows,n,lambda item:vector(item,heads,lambda v:vector(v,dim,lambda x:uint(x,255))))
        value = dict(target=dict(zip(sorted(ids),tensor(row['target'],len(ids)))))
        if not self.stage['tree']:
            draft_ids = row['draft_node_ids']
            if not isinstance(draft_ids,list) or any(type(n) is not int for n in draft_ids) or len(draft_ids)!=len(set(draft_ids)) or set(draft_ids)!=set(ids)-{max(ids)}:
                raise ValueError('invalid draft query rows')
            value['draft'] = dict(zip(draft_ids,tensor(row['draft'],len(draft_ids))))
        previous = self.stage['queries'].get(rank)
        if previous is not None and previous != value:
            raise ValueError('conflicting query retransmission')
        self.stage['queries'][rank] = value

    def rotate(self, values, position):
        result = []
        for i in range(0,len(values),2):
            angle = position/self.spec['rope_scale']/(self.spec['rope_theta']**(i/self.spec['head_dim']))
            a,b = values[i:i+2]
            result.extend([a*math.cos(angle)-b*math.sin(angle),a*math.sin(angle)+b*math.cos(angle)])
        return result

    def logits(self, kind, node, pages):
        spec = self.spec
        pos = self.stage['positions'][node]
        table = [r for r in self.stage['table'] if r['position']<=pos and
                 (r['position']>=pos-spec['window_size']+1 or r['position']<spec['sink_tokens'])]
        if not table:
            raise ValueError('empty causal attention context')
        context = [None]*len(spec['head_map'])
        dim = spec['head_dim']
        for rank, part in enumerate(spec['ranks']):
            vectors = self.stage['queries'][rank][kind][node]
            for local,head in enumerate(part['query_heads']):
                query = self.rotate([(x-part['zero_point'])*part['scale'] for x in vectors[local]],pos)
                scores, values = [], []
                kvhead = spec['head_map'][head]
                for row in table:
                    page = pages[row['page_id'],row['version']]
                    offset = (row['slot']*spec['kv_heads']+kvhead)*dim
                    channel = kvhead*dim
                    key = [(page['k'][offset+i]-page['k_zero'][channel+i])*page['k_scale'][channel+i] for i in range(dim)]
                    val = [(page['v'][offset+i]-page['v_zero'][channel+i])*page['v_scale'][channel+i] for i in range(dim)]
                    key = self.rotate(key,row['position'])
                    scores.append(sum(a*b for a,b in zip(query,key))/math.sqrt(dim)+spec['alibi_slopes'][head]*(row['position']-pos))
                    values.append(val)
                maximum = max(scores)
                weights = [math.exp(s-maximum) for s in scores]
                weights = [w/sum(weights[j] for j,r in enumerate(table) if (r['page_id'],r['version'])==(table[i]['page_id'],table[i]['version'])) for i,w in enumerate(weights)]
                total = sum(weights)
                context[head] = [sum(w*v[i] for w,v in zip(weights,values))/total for i in range(dim)]
        flattened = [x for part in spec['ranks'] for head in part['query_heads'] for x in context[head]]
        return [spec['bias'][token]+sum((w-spec['projection_zero'][token])*spec['projection_scale'][token]*x
                                       for w,x in zip(row,flattened)) for token,row in enumerate(spec['projection'])]

    def prepare(self):
        if self.stage is None or set(self.stage['queries']) != set(range(len(self.spec['ranks']))):
            raise ValueError('incomplete attention rank set')
        pages = self.cache.copy()
        pages.update(self.stage['pages'])
        keys = {(r['page_id'],r['version']) for r in self.stage['table']}
        if not keys <= pages.keys():
            raise ValueError('logical map references unavailable page version')
        nodes = sorted(self.stage['positions'])
        targets = {n:self.logits('target',n,pages) for n in nodes}
        drafts = {} if self.stage['tree'] else {n:self.logits('draft',n,pages) for n in nodes[:-1]}
        shards = []
        for rank,part in enumerate(self.asset['shards']):
            encode = lambda logits:[max(0,min(255,round(logits[t]/part['scale']+part['zero_point']))) for t in part['token_ids']]
            row = dict(rank=rank,target=[encode(targets[n]) for n in nodes])
            if self.stage['tree']:
                row['node_ids'] = nodes
            else:
                row['draft'] = [encode(drafts[n]) for n in nodes[:-1]]
            shards.append(row)
        self.stage['next_cache'] = {k:pages[k] for k in keys}
        self.stage['query_rows'] = len(targets)+len(drafts)
        return shards

    def publish(self):
        self.cache.update(self.stage['next_cache'])
        self.allocate(len(self.cache))
        self.report.update(resident_pages=len(self.cache),staged_pages=0,
                           committed_windows=self.report['committed_windows']+1,
                           query_rows=self.report['query_rows']+self.stage['query_rows'],
                           last_positions=sorted(set(self.stage['positions'].values())))
        self.stage = None

    def cleanup(self):
        self.cache.clear()
        self.stage = None
        self.allocate(0)
        self.report.update(resident_pages=0,staged_pages=0)

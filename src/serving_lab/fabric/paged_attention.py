"""Paged decoder window lifecycle."""
from .paged_layout import validate_asset, uint
from .kv_pages import PageStorage
from .attention_math import AttentionMath


def empty_report(revision):
    return dict(revision=revision, resident_pages=0, staged_pages=0, committed_windows=0,
                query_rows=0, last_positions=[])


class PageContext(PageStorage, AttentionMath):
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
        self.stage = dict(table=sorted(mapped,key=lambda r:r['position']),positions=positions,
                          tree='tree' in window,pages={},queries={})

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
        self.stage['next_cache'] = pages
        self.stage['query_rows'] = len(targets)+len(drafts)
        return shards

    def publish(self):
        self.cache = self.stage['next_cache']
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

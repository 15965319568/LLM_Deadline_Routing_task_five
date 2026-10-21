"""Paged decoder window lifecycle."""
from .paged_layout import validate_asset, uint
from .kv_pages import PageStorage
from .attention_math import AttentionMath
from .paged_view import view_for
from .retention_policy import retain_images, inventory_cost
from .striped_pages import StripedStorage


def empty_report(revision):
    return dict(revision=revision, resident_pages=0, staged_pages=0, committed_windows=0,
                query_rows=0, last_positions=[], published_slots=0, accepted_frontier=0)


class PageContext(StripedStorage, PageStorage, AttentionMath):
    def __init__(self, asset, allocate, directory=None, owner=None, scope=None):
        self.asset = asset
        self.spec = asset['paged_attention']
        self.allocate = allocate
        self.directory, self.owner = directory, owner
        if directory is not None: directory.register(owner, self, scope)
        self.cache = {}
        self.view = {}
        self.stage = None
        self.report = empty_report(self.spec['revision'])

    def begin(self, row, window):
        if self.stage is not None:
            raise ValueError('overlapping paged window')
        recovery = row.get('kv_recovery', 'full')
        if recovery not in ['full', 'view']:
            raise ValueError('invalid recovery mode')
        ids = {0, *window['tree']} if 'tree' in window else set(range(len(window['proposal'])+1))
        raw = row['query_positions']
        if not isinstance(raw, dict) or set(raw) != {str(i) for i in ids}:
            raise ValueError('query positions must identify every query row')
        positions = {int(k):uint(v) for k,v in raw.items()}
        mapped = view_for(row, self.view, self.report['committed_windows'], self.spec)
        self.stage = dict(recovery=recovery,table=sorted(mapped,key=lambda r:r['position']),positions=positions,
                          tree='tree' in window,pages={},queries={},transfers={},imports=row.get('kv_imports',[]),retained=row.get('kv_keep',[]))

    def prepare(self):
        if self.stage is None or set(self.stage['queries']) != set(range(len(self.spec['ranks']))):
            raise ValueError('incomplete attention rank set')
        if self.stage['imports']:
            self.stage['pages'].update(self.directory.resolve(self.stage['imports']))
        pages = self.images()
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

    def publish(self, frontier=0):
        self.cache = retain_images(self.stage['next_cache'],self.stage['table'],self.stage['retained'])
        self.view = {r['position']:r.copy() for r in self.stage['table']}
        self.allocate(inventory_cost(self.cache))
        self.report.update(resident_pages=len(self.cache),staged_pages=0,
                           committed_windows=self.report['committed_windows']+1,
                           query_rows=self.report['query_rows']+self.stage['query_rows'],
                           last_positions=sorted(set(self.stage['positions'].values())),
                           published_slots=len(self.cache)*self.spec['page_size'],accepted_frontier=frontier)
        self.stage = None

    def cleanup(self):
        if self.directory is not None: self.directory.discard(self.owner)
        self.cache.clear()
        self.view.clear()
        self.stage = None
        self.allocate(0)
        self.report.update(resident_pages=0,staged_pages=0,published_slots=0)

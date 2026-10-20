"""Quantized page storage and query row assembly."""
import copy
from .paged_layout import uint, real, vector
from .kv_fragments import Transfer
from .kv_repack import Repack


class PageStorage:
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
            result.extend(pair)
        return result

    def page(self, row):
        if self.stage is None:
            raise ValueError('page outside window')
        name = row['page_id']
        if not isinstance(name,str) or not name:
            raise ValueError('invalid page identity')
        version = uint(row['version'], minimum=1)
        if 'encoding' in row:
            if row['encoding'] not in ['kv-fragments/v1','kv-fragments/v2']:
                raise ValueError('unknown page encoding')
            key = name, version
            factory = Repack if row['encoding']=='kv-fragments/v2' else Transfer
            transfer = copy.deepcopy(self.stage['transfers'].get(key, factory(self.spec)))
            transfer.record(row)
            if transfer.manifest and transfer.manifest['base_version'] >= version:
                raise ValueError('transfer base must be an older version')
            self.allocate(len(set(self.stage['pages'])|{key}))
            self.stage['transfers'][key] = transfer
            self.report['staged_pages'] = len((set(self.stage['pages'])|set(self.stage['transfers']))-set(self.cache))
            return
        base = uint(row['base_version'])
        width = self.spec['kv_heads']*self.spec['head_dim']
        if base == 0:
            if 'patches' in row:
                raise ValueError('full page also declares deltas')
            value = {kind:self.unpack(row[kind]) for kind in ['k','v']}
            for kind in ['k','v']:
                value[kind+'_scale'] = vector(row[kind+'_scale'],width,lambda x:real(x,True))
                value[kind+'_zero'] = vector(row[kind+'_zero'],width,lambda x:uint(x,15))
                value[kind+'_after'] = list(row.get(kind+'_after',[0]*len(value[kind])))
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
        self.report['staged_pages'] = len((set(self.stage['pages'])|set(self.stage['transfers']))-set(self.cache))

    def images(self):
        images = self.cache.copy()
        images.update(self.stage['pages'])
        completed = set()

        def materialize(key):
            if key in completed or key not in self.stage['transfers']:
                if key not in images:
                    raise ValueError('unavailable transfer parent')
                return images[key]
            transfer = self.stage['transfers'][key]
            value = transfer.resolve_images(lambda source:images[source]) if isinstance(transfer,Repack) else transfer.resolve(lambda base: self.cache[key[0], base])
            if key in images and images[key] != value:
                raise ValueError('wire encodings disagree on page image')
            images[key] = value
            completed.add(key)
            return value

        for key in sorted(self.stage['transfers'],key=lambda key:key[1]):
            materialize(key)
        return images

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

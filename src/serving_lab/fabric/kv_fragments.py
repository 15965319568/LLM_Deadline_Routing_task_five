"""Incremental transfer image assembly used by the decode fast path."""
import copy
from .paged_layout import uint, real, vector


class Transfer:
    def __init__(self, spec):
        self.spec = spec
        self.manifest = None
        self.parts = {}

    def record(self, row):
        if row['record'] == 'manifest':
            self.manifest = dict(row)
            return
        if row['record'] != 'span':
            raise ValueError('unknown transfer record')
        identity = row['id']
        revision = uint(row['revision'], minimum=1)
        current = self.parts.get(identity)
        if current is not None and revision < current['revision']:
            return
        values = []
        for byte in bytes.fromhex(row['data']):
            values.extend([byte & 15, byte >> 4])
        self.parts[identity] = dict(revision=revision, tensor=row['tensor'],
                                    offset=uint(row['offset']), values=values[:uint(row['count'])])

    def resolve(self, parent):
        if self.manifest is None:
            raise ValueError('missing transfer manifest')
        meta = self.manifest
        length = self.spec['page_size']*self.spec['kv_heads']*self.spec['head_dim']
        if meta['base_version']:
            image = dict(parent(meta['base_version']))
        else:
            image = {t:[0]*length for t in ['k','v']}
            for t in ['k','v']:
                image[t+'_scale'] = meta[t+'_scale']
                image[t+'_zero'] = meta[t+'_zero']
        for part in self.parts.values():
            begin = part['offset']
            image[part['tensor']][begin:begin+len(part['values'])] = part['values']
        return image

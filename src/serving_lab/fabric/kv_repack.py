"""Worker-side gather representation accepted by the compacting decoder."""
from .kv_fragments import Transfer


class Repack(Transfer):
    def __init__(self, spec):
        super().__init__(spec)
        self.gathers = {}

    def record(self, row):
        if row['record'] != 'gather':
            return super().record(row)
        current = self.gathers.get(row['id'])
        if current is None or row['revision'] >= current['revision']:
            self.gathers[row['id']] = dict(row)

    def resolve_images(self, resolve):
        result = self.resolve(lambda base:None)
        width = self.spec['kv_heads']*self.spec['head_dim']
        for row in self.gathers.values():
            ref=row['source'];t=row['tensor']
            src=resolve((ref['page_id'],ref['version']))
            for j in range(row['count']):
                dst=row['offset']+j;index=ref['offset']+j*abs(ref['stride'])
                channel=dst%width
                value=(src[t][index%len(src[t])]-src[t+'_zero'][channel])*src[t+'_scale'][channel]
                code=int(value/result[t+'_scale'][channel]+result[t+'_zero'][channel]+.5)
                result[t][dst]=max(0,min(15,code))
        return result

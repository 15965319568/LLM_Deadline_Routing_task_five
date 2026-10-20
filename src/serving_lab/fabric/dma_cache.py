"""Reusable worker upload cache for homogeneous DMA buffers."""
from .dma_unpack import decode
from .recovery_lanes import ingest,materialize
from . import replica_cache

class UploadCache:
    def __init__(self,spec):
        self.spec=spec;self.buffers={}
    def put(self,row):
        entry=self.buffers.setdefault(row['block_id'],dict(chunks={},directory=None))
        if row['record']=='replica':
            replica_cache.put(entry,row);return
        if row['record']=='repair':
            ingest(entry,row);return
        if row['record']=='directory':entry['directory']=dict(row)
        else:
            old=entry['chunks'].get(row['id'])
            if old is None or row['revision']>=old['revision']:entry['chunks'][row['id']]=dict(row)
    def get(self,block_id,epoch):
        entry=self.buffers[block_id];meta=entry['directory']
        result=dict(meta,data=[0]*meta['length'],after=[0]*meta['length'])
        for row in entry['chunks'].values():
            start=row['offset'];data=decode(row,self.spec)
            result['data'][start:start+len(data)]=data
            result['after'][start:start+len(data)]=[row.get('after',0)]*len(data)
        replica_cache.materialize(self.buffers,entry,result)
        return result
    def clear(self):self.buffers.clear()

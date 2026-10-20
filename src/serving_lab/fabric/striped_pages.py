"""Connect DMA uploads to the existing page-oriented decode path."""
from .dma_cache import UploadCache
from .stripe_layout import expand
from .stripe_residency import workspace_pages

class StripedStorage:
    def block(self,row):
        if self.stage is None:raise ValueError('DMA outside decode window')
        if not hasattr(self,'uploads'):self.uploads=UploadCache(self.spec)
        self.uploads.put(row)
    def page(self,row):
        if row.get('encoding')!='kv-striped/v1':return super().page(row)
        if not hasattr(self,'uploads'):self.uploads=UploadCache(self.spec)
        self.stage.setdefault('striped',{})[row['page_id'],row['version']]=dict(row)
    def images(self):
        if self.stage.get('striped'):
            for key,meta in self.stage['striped'].items():self.stage['pages'][key]=expand(meta,self.uploads,self.spec)
            self.allocate(workspace_pages(self.stage['pages'],self.uploads))
        return super().images()

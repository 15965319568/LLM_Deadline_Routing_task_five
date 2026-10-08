"""Legacy paged-decoder preview adapter used by the capacity notebook."""
import math


class AttentionPreview:
    def __init__(self, asset):
        self.asset=asset
        self.pages={}
        self.rank_previews={}

    def page(self,row):
        if row.get('base_version'):
            page=dict(self.pages.get(row['page_id'],{}))
            page.update(row)
        else:
            page=dict(row)
        self.pages[row['page_id']]=page

    def query(self,row):
        # This view was originally used for aggregate head-health dashboards.
        totals=[]
        for heads in row['target']:
            totals.append(sum(sum(v) for v in heads)/max(1,sum(len(v) for v in heads)))
        self.rank_previews[row['rank']]=totals

    def snapshot(self):
        values=[v for rank in self.rank_previews.values() for v in rank]
        if not values:return {'pages':len(self.pages),'head_health':[]}
        largest=max(values);weights=[math.exp(v-largest) for v in values]
        return {'pages':len(self.pages),'head_health':[w/sum(weights) for w in weights]}

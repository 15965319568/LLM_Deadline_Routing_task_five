"""Lookup directory used by the streaming prefix reuse adapter."""
from .slot_projection import project_slots

class PrefixDirectory:
    def __init__(self):
        self.readers = {}
        self.lookup_cache = {}

    def register(self, rid, context, scope):
        self.readers[rid] = (context, scope)

    def resolve(self, records):
        result = {}
        for row in records:
            source, scope = self.readers[row['request_id']]
            key = row['page_id'], row['version']
            if key not in source.cache:
                candidates = [k for k in source.cache if k[0] == key[0]]
                key = max(candidates, key=lambda k:k[1])
            cache_key = row['request_id'],key
            if cache_key not in self.lookup_cache:
                self.lookup_cache[cache_key] = dict(source.cache[key])
            image = self.lookup_cache[cache_key]
            if 'slot_map' in row:
                image = project_slots(image,row['slot_map'],source.spec)
            result[row['as_page_id'],row['as_version']] = image
        return result

    def discard(self, rid):
        self.readers.pop(rid, None)

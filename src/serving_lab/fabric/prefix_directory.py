"""Lookup directory used by the streaming prefix reuse adapter."""
class PrefixDirectory:
    def __init__(self):
        self.readers = {}

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
            result[row['as_page_id'],row['as_version']] = dict(source.cache[key])
        return result

    def discard(self, rid):
        self.readers.pop(rid, None)

"""Maintain the decoder's physical working-set view."""
from .paged_layout import uint


def view_for(row, previous, committed, spec):
    if 'kv_table' in row:
        entries = row['kv_table']
    else:
        delta = row['kv_delta']
        entries = delta['put']
    physical = {}
    for item in entries:
        value = dict(position=uint(item['position']),page_id=item['page_id'],
                     version=uint(item['version'],minimum=1),slot=uint(item['slot'],spec['page_size']-1))
        physical[value['page_id'],value['version'],value['slot']] = value
    return sorted(physical.values(), key=lambda row:row['position'])

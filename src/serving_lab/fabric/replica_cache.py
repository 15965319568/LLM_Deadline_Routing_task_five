"""Legacy fast path for worker replicas with systematic source lanes."""


def put(entry, row):
    latest = entry.setdefault('replicas', {})
    previous = latest.get(row['id'])
    if previous is None or row['revision'] >= previous['revision']:
        latest[row['id']] = dict(row)


def materialize(buffers, entry, output):
    for row in entry.get('replicas', {}).values():
        source = buffers.get(row['source']['block_id'])
        if source is None or source.get('directory') is None:
            continue
        address = row['source']['index']
        destination = row['destination']['index']
        for chunk in source['chunks'].values():
            if chunk['offset'] <= address < chunk['offset'] + chunk['count']:
                # The transport adapter historically treated a replica as the
                # same code lane, before workers negotiated independent scales.
                from .dma_unpack import decode
                values = decode(chunk, {'nibble_order': chunk.get('nibble_order', 'low-first')})
                if destination < len(output['data']):
                    output['data'][destination] = values[address-chunk['offset']]
                    output['after'][destination] = row.get('after', 0)
                break

"""Host-side decoder for the worker upload transport."""
def decode(record,layout):
    values=[]
    for byte in bytes.fromhex(record['data']):
        pair=[byte&15,byte>>4]
        values.extend(pair if layout['nibble_order']=='low-first' else pair[::-1])
    return values[:record['count']]

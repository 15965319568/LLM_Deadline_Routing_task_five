"""Versioned int4 KV workspaces and row-parallel grouped-query attention."""
import copy
import math


def uint(x, maximum=None, minimum=0):
    if type(x) is not int or x < minimum or maximum is not None and x > maximum:
        raise ValueError('invalid unsigned integer')
    return x


def real(x, positive=False):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or positive and x <= 0:
        raise ValueError('invalid finite scalar')
    return float(x)


def vector(xs, size, convert):
    if not isinstance(xs, list) or len(xs) != size:
        raise ValueError('tensor dimension mismatch')
    return [convert(x) for x in xs]


def validate_asset(raw, vocabulary):
    dim = uint(raw['head_dim'], 16, 2)
    if dim % 2:
        raise ValueError('RoPE requires an even head dimension')
    kv = uint(raw['kv_heads'], 8, 1)
    heads = raw['head_map']
    if not isinstance(heads, list) or not 1 <= len(heads) <= 16:
        raise ValueError('invalid GQA head map')
    heads = [uint(h, kv-1) for h in heads]
    out = dict(revision=uint(raw['revision'], minimum=1), head_dim=dim, kv_heads=kv, head_map=heads,
               page_size=uint(raw['page_size'], 16, 1), window_size=uint(raw['window_size'], minimum=1),
               sink_tokens=uint(raw['sink_tokens']), rope_theta=real(raw['rope_theta'], True),
               rope_scale=real(raw['rope_scale'], True), nibble_order=raw['nibble_order'])
    if out['rope_theta'] <= 1 or out['nibble_order'] not in ['low-first', 'high-first']:
        raise ValueError('invalid rotary or packed layout')
    out['alibi_slopes'] = vector(raw['alibi_slopes'], len(heads), real)
    ranks = raw['ranks']
    if not isinstance(ranks, list) or not ranks:
        raise ValueError('missing attention ranks')
    out['ranks'] = []
    owned = []
    for part in ranks:
        ids = part['query_heads']
        if not isinstance(ids, list) or not ids:
            raise ValueError('empty rank')
        ids = [uint(h, len(heads)-1) for h in ids]
        owned.extend(ids)
        out['ranks'].append(dict(query_heads=ids, scale=real(part['scale'], True), zero_point=uint(part['zero_point'], 255)))
    if sorted(owned) != list(range(len(heads))):
        raise ValueError('query heads must be partitioned once')
    width = len(heads)*dim
    out['projection'] = vector(raw['projection'], vocabulary, lambda row: vector(row, width, lambda x: uint(x,255)))
    out['projection_scale'] = vector(raw['projection_scale'], vocabulary, lambda x: real(x,True))
    out['projection_zero'] = vector(raw['projection_zero'], vocabulary, lambda x: uint(x,255))
    out['bias'] = vector(raw['bias'], vocabulary, real)
    return out

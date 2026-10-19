"""Canonicalize independently reordered tensor rows for a draft tree."""
from .constraints import integer


def begin_tree(row, vocabulary_size):
    nodes=row['tree']; tape=row['uniforms']
    if 'proposal' in row or not isinstance(nodes,list) or not 1<=len(nodes)<=32 or not isinstance(tape,dict):
        raise ValueError('invalid draft tree')
    tree={}; children={}
    for node in nodes:
        identity,parent,token=integer(node['id'],1),integer(node['parent']),integer(node['token'])
        if identity in tree or token>=vocabulary_size or (parent,token) in children: raise ValueError('duplicate tree identity/branch')
        tree[identity]=(parent,token); children[parent,token]=identity
    for identity in tree:
        seen=set(); current=identity
        while current:
            if current in seen or current not in tree: raise ValueError('cycle or disconnected tree')
            seen.add(current)
            if len(seen)>8: raise ValueError('tree depth exceeds protocol')
            current=tree[current][0]
    if set(tape)!={str(identity) for identity in [0,*tree]}: raise ValueError('tree random tape identity mismatch')
    for value in tape.values():
        if type(value) not in (int,float) or not 0<=value<1: raise ValueError('invalid tree uniform')
    return dict(tree=tree,children=children,uniforms=dict(tape),shards={})


def shard_tree(row,current,asset):
    rank=integer(row['rank'])
    if rank>=len(asset['shards']): raise ValueError('unknown tree rank')
    identities=row['node_ids']; matrix=row['target']
    if not isinstance(identities,list) or not isinstance(matrix,list) or len(identities)!=len(matrix): raise ValueError('tree tensor dimensions')
    for identity in identities: integer(identity)
    if len(set(identities))!=len(identities) or set(identities)!={0,*current['tree']}: raise ValueError('missing/duplicate tree row')
    width=len(asset['shards'][rank]['token_ids'])
    normalized={}
    for identity,vector in zip(identities,matrix):
        if not isinstance(vector,list) or len(vector)!=width or any(integer(value)>255 for value in vector): raise ValueError('tree tensor value/width')
        normalized[identity]=vector
    if rank in current['shards'] and current['shards'][rank]!=normalized: raise ValueError('conflicting tree rank')
    current['shards'][rank]=normalized

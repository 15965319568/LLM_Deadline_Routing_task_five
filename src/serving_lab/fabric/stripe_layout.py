"""Convert a DMA map into the packed attention consumer's page representation."""
def expand(meta,cache,spec):
    width=spec['kv_heads']*spec['head_dim'];size=spec['page_size']*width
    image={t+s:list(meta[t+s]) for t in ['k','v'] for s in ['_scale','_zero']}
    image.update(k=[0]*size,v=[0]*size,k_after=[0]*size,v_after=[0]*size)
    for run in meta['runs']:
        block=cache.get(run['block_id'],run['epoch']);t=run['tensor']
        for j in range(run['count']):
            dest=run['offset']+j;source=run['start']+j*run['stride'];channel=dest%block['channels']
            real=(block['data'][source]-block['zero'][channel])*block['scale'][channel]
            image[t][dest]=max(0,min(15,round(real/image[t+'_scale'][dest%width]+image[t+'_zero'][dest%width])))
            image[t+'_after'][dest]=run.get('after',0)
    if 'valid_slots' in meta:image['valid_slots']=list(meta['valid_slots'])
    return image

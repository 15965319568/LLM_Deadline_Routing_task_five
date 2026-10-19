"""Prefix layout adapter used by the dense-worker compatibility path."""


def project_slots(image, slots, spec):
    if slots == list(range(spec['page_size'])):
        return dict(image)
    width = spec['kv_heads']*spec['head_dim']
    selection = sorted({s for s in slots if s is not None})
    selection += [s for s in range(spec['page_size']) if s not in selection]
    result = dict(image)
    for tensor in ['k','v']:
        result[tensor] = [value for slot in selection for value in image[tensor][slot*width:(slot+1)*width]]
    return result

"""Seed image adapter for inherited worker compactions."""
from .slot_projection import project_slots


def seed_image(meta, resolve, spec):
    source = meta.get('inherit')
    if source is None:
        return None
    result = project_slots(resolve((source['page_id'],source['version'])),
                           source.get('slot_map',list(range(spec['page_size']))),spec)
    for tensor in ['k','v']:
        # Preserve encoded cells for the legacy equal-layout worker stream.
        result[tensor+'_scale'] = meta[tensor+'_scale']
        result[tensor+'_zero'] = meta[tensor+'_zero']
    return result

"""Versioned route topology used by both admission and phase acks."""
import copy


def initial_routes(config):
    routes = {}
    for spec in config.get('route_specs', []):
        path = tuple(spec.get('path', ()))
        if len(path) == 3 and spec.get('route_id'):
            routes[spec['route_id']] = dict(spec, path=list(path))
    return routes


def apply_updates(routes, updates, topology_epoch):
    result = copy.deepcopy(routes)
    for update in updates or []:
        route_id = update.get('route_id')
        if route_id not in result:
            continue
        current = result[route_id]
        generation = update.get('generation')
        # A generation is a one-way fence.  Equal-generation updates are
        # ambiguous when two control-plane writers disagree, so retain the
        # already observed value and let the next generation resolve it.
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= current.get('generation', 0):
            continue
        current.update({k: update[k] for k in ('generation', 'enabled', 'draining') if k in update})
        current['admission_epoch'] = topology_epoch
    return result


def route_for_path(routes, path):
    for route_id, spec in routes.items():
        if tuple(spec.get('path', ())) == tuple(path):
            return route_id, spec
    return None, None


def usable(spec):
    return bool(spec and spec.get('enabled', False) and not spec.get('draining', False))

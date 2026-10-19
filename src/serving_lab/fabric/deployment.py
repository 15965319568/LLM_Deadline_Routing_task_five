"""Join serving deployment facts without promoting a ready shadow or stale replica."""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from serving_lab.storage import records, read_json

FIELDS = ('model_revision', 'tokenizer_revision', 'quantization', 'kv_schema', 'adapter', 'generation', 'config_hash')


def _int(value):
    if isinstance(value, bool): raise ValueError('boolean')
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != int(number): raise ValueError('nonnegative integer')
    return int(number)


def normalize(row):
    kind = row['source_kind']
    if kind not in {'registry', 'runtime', 'rollout'}: raise ValueError('source kind')
    value = {key: row[key] for key in ('resource', 'deployment_id', *FIELDS)}
    for key, item in value.items():
        if key != 'generation' and (not isinstance(item, str) or not item): raise ValueError(key)
    value['generation'] = _int(value['generation'])
    value.update(source_kind=kind, effective_us=_int(row['effective_us']))
    if kind == 'runtime':
        value['loaded_us'] = _int(row['loaded_us'])
        if type(row['ready']) not in (bool, str) or row['ready'] not in (True, False, 'true', 'false', 'True', 'False'): raise ValueError('ready')
        value['ready'] = row['ready'] is True or row['ready'] in ('true', 'True')
    if kind == 'rollout':
        if row['state'] not in {'active', 'shadow', 'retired'}: raise ValueError('state')
        value['state'] = row['state']
    return value


def resolve_deployments(root, manifest, config, as_of_us):
    states, groups = {}, defaultdict(list)
    for source, raw in records(root, manifest.get('deployments', [])):
        try:
            if _int(raw['ingested_us']) > as_of_us:
                states[source] = 'deferred'; continue
            row = normalize(raw)
            if row['resource'] not in config['resources']: raise ValueError('unknown resource')
            groups[(row['resource'], row['deployment_id'], row['source_kind'])].append((source, row))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            states[source] = 'invalid'
    chosen, origins = {}, {}
    for key, copies in groups.items():
        if any(row != copies[0][1] for _, row in copies):
            states.update((source, 'conflict') for source, _ in copies); continue
        source, row = min(copies, key=lambda pair: pair[0])
        chosen[key], origins[key] = row, source
        states.update((s, 'candidate' if s == source else 'duplicate') for s, _ in copies)
    candidates = []
    for (resource, identity, kind), registry in chosen.items():
        if kind != 'registry': continue
        try:
            runtime, rollout = (chosen[(resource, identity, k)] for k in ('runtime', 'rollout'))
            if any(peer[field] != registry[field] for peer in (runtime, rollout) for field in FIELDS):
                raise ValueError('cross-source mismatch')
            effective = max(peer['effective_us'] for peer in (registry, runtime, rollout))
            if not runtime['ready'] or runtime['loaded_us'] > as_of_us or effective > as_of_us or rollout['state'] != 'active':
                raise ValueError('not active and loaded')
            expected = config['resources'][resource]
            if any(registry[field] != expected[field] for field in ('model_revision','tokenizer_revision','quantization','adapter','kv_schema')):
                raise ValueError('unsupported serving contract')
            candidates.append(dict(resource=resource, deployment_id=identity, effective_us=effective,
                                   **{field: registry[field] for field in FIELDS}))
        except (KeyError, TypeError, ValueError):
            states[origins[(resource, identity, kind)]] = 'excluded'
    active = {}
    for row in candidates:
        old = active.get(row['resource'])
        if old is None or (row['effective_us'],row['generation'],row['deployment_id']) > (old['effective_us'],old['generation'],old['deployment_id']):
            active[row['resource']] = row
    return dict(resources=active, rows=[dict(source=s, disposition=states[s]) for s in sorted(states)])


def load_deployment(directory, config, now):
    document = read_json(Path(directory) / 'deployment-profile.json')
    if _int(document['as_of_us']) > now or set(document['resources']) != set(config['resources']):
        raise ValueError('future or incomplete deployment profile')
    for resource, expected in config['resources'].items():
        row = document['resources'][resource]
        if any(row.get(field) != expected[field] for field in ('model_revision','tokenizer_revision','quantization','adapter','kv_schema')):
            raise ValueError('deployment contract mismatch')
        if row['resource'] != resource or _int(row['effective_us']) > now or _int(row['generation']) < 0:
            raise ValueError('deployment identity')
        if not all(isinstance(row.get(f),str) and row[f] for f in ('deployment_id','config_hash')):
            raise ValueError('deployment identity')
    return {resource:{key:row[key] for key in ('resource','deployment_id','effective_us',*FIELDS)} for resource,row in document['resources'].items()}

"""Resolve visible, enacted tenant amendments before publishing a profile bundle."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from serving_lab.storage import read_json, records
from .evidence import integer, number, row_signature

INTEGER_FIELDS = {'max_slots', 'max_pages', 'max_priority', 'max_starts', 'start_window_us'}
VALUE_FIELDS = {'max_work_us', 'burst_credit_us', 'burst_refill_us', 'failure_penalty_us'}


def compile_control(root, config, as_of):
    root = Path(root)
    manifest = read_json(root / 'control/manifest.json')
    if manifest.get('schema') != 'tenant-control/v1':
        raise ValueError('unknown control schema')
    for name, checksum in manifest['sources'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('control source checksum mismatch')
    groups, states = defaultdict(list), {}
    for source, row in records(root, manifest['sources']):
        states[source] = 'invalid'
        try:
            if row['schema'] != manifest['schema'] or row['tenant'] not in config['tenant_limits']:
                continue
            if row['signature'] != row_signature({k: v for k, v in row.items() if k != 'signature'}):
                continue
            revision, parent = integer(row['revision']), integer(row['parent'])
            effective, ingested = integer(row['effective_us']), integer(row['ingested_us'])
            patch = json.loads(row['patch'])
            if not revision > parent or not isinstance(patch, dict) or not patch or set(patch) - INTEGER_FIELDS - VALUE_FIELDS:
                continue
            patch = {k: integer(v) if k in INTEGER_FIELDS else float(number(v)) for k, v in patch.items()}
            if ingested > as_of:
                states[source] = 'deferred'
            elif row['authority'] != 'controller' or row['state'] != 'enacted':
                states[source] = 'excluded'
            elif effective > as_of:
                states[source] = 'pending'
            else:
                groups[row['tenant'], revision].append((source, parent, effective, patch))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    policies = {tenant: dict(revision=0, limits=dict(limits)) for tenant, limits in config['tenant_limits'].items()}
    for (tenant, revision), copies in sorted(groups.items()):
        values = {json.dumps([parent, effective, patch], sort_keys=True) for _, parent, effective, patch in copies}
        if len(values) != 1:
            states.update((source, 'conflict') for source, *_ in copies)
            continue
        chosen, parent, _, patch = min(copies, key=lambda item: item[0])
        states.update((source, 'duplicate') for source, *_ in copies)
        if parent != policies[tenant]['revision']:
            states[chosen] = 'orphan'
            continue
        policies[tenant] = dict(revision=revision, limits=dict(policies[tenant]['limits'], **patch))
        states[chosen] = 'applied'
    return {'tenant-policies': dict(as_of_us=as_of, tenants=policies),
            'tenant-policy-ledger': dict(rows=[dict(source=s, disposition=d) for s, d in sorted(states.items())])}


def load_control(directory, config, now):
    document = read_json(Path(directory) / 'tenant-policies.json')
    if integer(document['as_of_us']) > now or set(document['tenants']) != set(config['tenant_limits']):
        raise ValueError('future or incomplete tenant policies')
    policies = {}
    for tenant, row in document['tenants'].items():
        revision, limits = integer(row['revision']), row['limits']
        if set(limits) - INTEGER_FIELDS - VALUE_FIELDS or not set(config['tenant_limits'][tenant]) <= set(limits):
            raise ValueError('invalid tenant policy shape')
        limits = {k: integer(v) if k in INTEGER_FIELDS else float(number(v)) for k, v in limits.items()}
        policies[tenant] = dict(revision=revision, limits=limits)
    return policies

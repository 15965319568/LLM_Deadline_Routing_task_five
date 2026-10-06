"""Notebook-era controller import used by the running pilot."""
from pathlib import Path
from serving_lab.storage import read_json, records
import json


def compile_control(root, config, as_of):
    root = Path(root)
    manifest = read_json(root / 'control/manifest.json')
    tenants = {t: dict(revision=0, limits=dict(l)) for t, l in config['tenant_limits'].items()}
    ledger = {}
    for source, row in records(root, manifest['sources']):
        ledger[source] = 'invalid'
        try:
            if int(row['ingested_us']) > as_of or int(row['effective_us']) > as_of:
                ledger[source] = 'deferred'
                continue
            tenant, revision = row['tenant'], int(row['revision'])
            patch = json.loads(row['patch'])
            if revision < tenants[tenant]['revision']:
                ledger[source] = 'duplicate'
                continue
            tenants[tenant]['limits'].update(patch)
            tenants[tenant]['revision'] = revision
            ledger[source] = 'applied'
        except (KeyError, ValueError, TypeError):
            continue
    return {'tenant-policies': dict(as_of_us=as_of, tenants=tenants),
            'tenant-policy-ledger': dict(rows=[dict(source=s, disposition=d) for s, d in sorted(ledger.items())])}


def load_control(directory):
    return read_json(Path(directory) / 'tenant-policies.json')['tenants']

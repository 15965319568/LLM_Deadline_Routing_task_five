"""Semantic digests bind measurements and tenant policies to one reload unit."""
import hashlib
import json
from pathlib import Path
from serving_lab.storage import read_json

ARTIFACTS = ('fabric-profiles', 'phase-ledger', 'phase-audit', 'phase-drift',
             'tenant-policies', 'tenant-policy-ledger')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def manifest(documents, as_of):
    artifacts = {name: digest(documents[name]) for name in ARTIFACTS}
    # The bundle id is the immutable fence used by the live control plane.  It
    # intentionally excludes filesystem names and JSON formatting so a copied
    # bundle has the same identity as the original export.
    identity = digest(dict(schema='profile-bundle/v1', as_of_us=as_of,
                           artifacts=artifacts))
    return dict(schema='profile-bundle/v1', as_of_us=as_of,
                artifacts=artifacts, bundle_id=identity,
                artifact_order=list(ARTIFACTS))


def verify(directory):
    root = Path(directory)
    record = read_json(root / 'bundle-manifest.json')
    if (record.get('schema') != 'profile-bundle/v1' or
            set(record.get('artifacts', {})) != set(ARTIFACTS) or
            record.get('artifact_order') != list(ARTIFACTS) or
            not isinstance(record.get('bundle_id'), str)):
        raise ValueError('invalid profile bundle manifest')
    documents = {name: read_json(root / (name+'.json')) for name in ARTIFACTS}
    if any(digest(documents[name]) != record['artifacts'][name] for name in ARTIFACTS):
        raise ValueError('profile bundle digest mismatch')
    if any(documents[name]['as_of_us'] != record['as_of_us'] for name in ['fabric-profiles', 'tenant-policies']):
        raise ValueError('mixed profile bundle cutoffs')
    expected = digest(dict(schema='profile-bundle/v1', as_of_us=record['as_of_us'],
                           artifacts=record['artifacts']))
    if record['bundle_id'] != expected:
        raise ValueError('profile bundle identity mismatch')
    return documents

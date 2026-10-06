"""Versioned artifacts consumed by the gateway factory."""
from .schema import fingerprint
from .storage import read_json


def load_profiles(directory, deployment):
    document = read_json(directory / 'profiles.json')
    records = {p['endpoint_id']:p for p in document['targets']}
    result = []
    for target in deployment['targets']:
        record = records[target['endpoint_id']]
        if fingerprint(record['fingerprint']) != fingerprint(target):
            raise ValueError('Profile deployment fingerprint mismatch')
        result.append(dict(target, usable=record['status']=='ready', surface=record['surface']))
    return result

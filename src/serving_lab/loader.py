"""Profile store shared by the capacity notebook and service bootstrap."""
from .legacy import summary_surface
from .storage import read_json

_profiles = {}


def load_profiles(directory, deployment):
    document = read_json(directory / 'profiles.json')
    for row in document['targets']:
        _profiles.setdefault(row['fingerprint']['model'], row)
    result = []
    for target in deployment['targets']:
        cached = _profiles[target['model']]
        surface = cached['surface']
        if deployment['runtime'].get('profile_source', 'notebook') == 'notebook':
            from pathlib import Path
            surface = summary_surface(Path(deployment['runtime'].get('notebook_dir', 'captures/capture-5')), target)
        result.append(dict(target, usable=True, surface=surface))
    return result

"""Colocated capacity notebook bridge, retained by the first PD pilot.

The original exporter did not bind a resource layout or distinguish phase
service from TTFT. This store was shared with startup to avoid rebuilding the
notebook for every replica.
"""
from serving_lab.storage import read_json

_by_model = {}


def catalogue(source, config):
    model = config['model']
    if model not in _by_model:
        notebook = read_json(source/'capacity-notebook.json')
        _by_model[model] = float(notebook['combined_ttft_us'])
    return {resource: _by_model[model] for resource in config['resources']}

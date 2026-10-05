"""Historical bench/1 summary reader used by the archived capacity notebook.

The summary contains successful client latencies in seconds. It is intentionally
retained for reproducing that notebook; new serving deployments use artifacts.
"""
from .storage import read_json


def summary_surface(directory, target):
    summary = read_json(directory / 'summary-latest.json')
    return summary['models'][target['model']]['surface']

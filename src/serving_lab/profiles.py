"""Convert imported measurements to the notebook's dense lookup tables."""
from collections import defaultdict
from statistics import median
from .schema import FINGERPRINT


def construct(deployment, samples, cutoff):
    policy = deployment['profile_policy']
    tables = []
    for target in deployment['targets']:
        cells = defaultdict(list)
        for sample in samples:
            if sample['endpoint_id'] != target['endpoint_id']:
                continue
            x = min(policy['tokens'], key=lambda t:abs(t-sample['tokens']))
            y = min(policy['decodes'], key=lambda d:abs(d-sample['decodes']))
            cells[(x,y)].append(sample['ttft_us'])
        values = [[float(median(cells[x,y])) if cells[x,y] else 0 for x in policy['tokens']] for y in policy['decodes']]
        counts = [[len(cells[x,y]) for x in policy['tokens']] for y in policy['decodes']]
        tables.append(dict(endpoint_id=target['endpoint_id'],fingerprint={k:target[k] for k in FINGERPRINT},
            status='ready', counts=counts, surface=dict(tokens=policy['tokens'],decodes=policy['decodes'],service_us=values)))
    return dict(as_of_us=cutoff,targets=tables)

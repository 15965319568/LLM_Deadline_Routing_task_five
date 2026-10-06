"""Cell support is part of a profile, not an optional quality annotation."""
from decimal import Decimal
from statistics import median
from .schema import FINGERPRINT


def construct(deployment, samples, cutoff):
    policy = deployment['profile_policy']
    result = {'as_of_us': cutoff, 'targets': []}
    for target in deployment['targets']:
        own = [s for s in samples if s['endpoint_id'] == target['endpoint_id'] and s['purpose'] == 'baseline']
        values, counts = [], []
        for decodes in policy['decodes']:
            costs, sizes = [], []
            for tokens in policy['tokens']:
                cell = [Decimal(str(s['service_us'])) for s in own if (s['tokens'], s['decodes']) == (tokens, decodes)]
                sizes.append(len(cell))
                costs.append(float(median(cell)) if len(cell) >= policy['min_samples'] else None)
            values.append(costs)
            counts.append(sizes)
        ready = all(v is not None for row in values for v in row)
        result['targets'].append(dict(endpoint_id=target['endpoint_id'], fingerprint={k:target[k] for k in FINGERPRINT},
            status='ready' if ready else 'insufficient_data', counts=counts,
            surface=dict(tokens=policy['tokens'], decodes=policy['decodes'], service_us=values)))
    return result

"""Compare service under equal work conditions, retaining workload mix changes."""
from decimal import Decimal
from vllm_router.deadline.profiles import Surface


def evaluate(deployment, profiles, samples):
    policy = deployment['profile_policy']
    result = {}
    for profile in profiles['targets']:
        eid = profile['endpoint_id']
        rows = []
        if profile['status'] == 'ready':
            surface = Surface(profile['surface'])
            for sample in samples:
                if sample['endpoint_id'] != eid or sample['purpose'] != 'recent':
                    continue
                if sample['tokens'] > surface.tokens[-1] or sample['decodes'] > surface.decodes[-1]:
                    continue
                cost = surface.estimate(sample['tokens'], sample['decodes'])
                if cost > 0:
                    rows.append((sample, Decimal(str(sample['service_us'])) / Decimal(str(cost))))
        rows.sort(key=lambda r: (r[0]['ended_us'], r[0]['identity']))
        rows = rows[-policy['recent_window']:]
        ratio = sum((r[1] for r in rows), Decimal(0)) / len(rows) if rows else None
        state = 'insufficient_data'
        if len(rows) == policy['recent_window']:
            state = 'slow' if ratio > Decimal(str(policy['drift_upper'])) else 'fast' if ratio < Decimal(str(policy['drift_lower'])) else 'normal'
        result[eid] = dict(state=state, count=len(rows), ratio=round(float(ratio), 6) if ratio is not None else None,
                           sample_ids=[r[0]['identity'] for r in rows])
    return result

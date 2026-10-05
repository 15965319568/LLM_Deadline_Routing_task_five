"""Notebook latency alarm over delivered recent results."""


def evaluate(deployment, profiles, samples):
    policy = deployment['profile_policy']
    result = {}
    for profile in profiles['targets']:
        eid = profile['endpoint_id']
        baseline = [s['ttft_us'] for s in samples if s['endpoint_id']==eid and s['purpose']=='baseline']
        recent = [s for s in samples if s['endpoint_id']==eid and s['purpose']=='recent']
        recent.sort(key=lambda s:(s['available_us'],s['identity']))
        recent = recent[-policy['recent_window']:]
        average = sum(baseline)/len(baseline) if baseline else 0
        ratio = sum(s['ttft_us'] for s in recent)/len(recent)/average if recent and average else None
        state = 'insufficient_data'
        if ratio is not None and len(recent)==policy['recent_window']:
            state = 'slow' if ratio>policy['drift_upper'] else 'fast' if ratio<policy['drift_lower'] else 'normal'
        result[eid] = dict(state=state,count=len(recent),ratio=round(ratio,6) if ratio is not None else None,
            sample_ids=[s['identity'] for s in recent])
    return result

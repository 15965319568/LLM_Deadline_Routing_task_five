"""Compile phase surfaces without trusting obsolete combined-TTFT summaries."""
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median
from serving_lab.storage import read_json, write_json
from .evidence import sources, grouped, number, integer, clock_time, prefix_pages, measurement_identity
from .deployment import resolve_deployments
from .constraints import compile_grammars


def compile_fabric(input_dir, output_dir, as_of_us):
    root, out = Path(input_dir), Path(output_dir)
    config = read_json(root/'fabric.json')
    manifest = read_json(root/'phase-exports.json')
    raw = sources(root, manifest)
    deployment = resolve_deployments(root, manifest, config, as_of_us)
    attempts, dispositions = grouped(raw['measurements'], measurement_identity, as_of_us)
    traces, _ = grouped(raw['traces'], lambda r: r['trace_id'], as_of_us)
    samples = []
    for source, row in raw['measurements']:
        if source not in dispositions:
            try:
                dispositions[source] = 'deferred' if integer(row['ingested_us']) > as_of_us else 'invalid'
            except (KeyError, ValueError, TypeError, ArithmeticError):
                dispositions[source] = 'invalid'
        if dispositions[source] != 'candidate':
            continue
        try:
            resource = config['resources'][row['resource']]
            if row['role'] != resource['role'] or row['layout'] != resource['layout']:
                raise ValueError('wrong serving conditions')
            if row['phase'] != 'steady' or row['status'] != 'ok':
                raise ValueError('not a steady success')
            span = traces[row['trace_id']]
            if span['resource'] != row['resource'] or span['sample_id'] != row['sample_id'] or integer(span['attempt']) != integer(row['attempt']):
                raise ValueError('trace belongs to a different attempt')
            multiplier = {'phase/1': Decimal(1000), 'phase/2': Decimal(1)}[row['schema']]
            reported = number(row['duration']) * multiplier
            start = clock_time(raw['anchors'], span['producer'], span['boot'], span['start_tick'], as_of_us)
            end = clock_time(raw['anchors'], span['producer'], span['boot'], span['end_tick'], as_of_us)
            if not start < end <= as_of_us or abs(reported-(end-start)) > config['duration_tolerance_us']:
                raise ValueError('clock span and exporter disagree')
            prompt = row['prompt']
            if not isinstance(prompt, str):
                raise ValueError('local tokenizer is Unicode codepoints')
            compatibility = {field: row.get(field, config.get('deployment_contract', {}).get(field)) for field in ('model_revision','tokenizer_revision','quantization','kv_schema','adapter')}
            deployment_row = deployment['resources'].get(row['resource'])
            if (deployment_row is None or row.get('deployment_id', deployment_row['deployment_id']) != deployment_row['deployment_id'] or
                    any(row.get(field, compatibility[field]) != deployment_row.get(field)
                        for field in ('model_revision','tokenizer_revision','quantization','adapter','kv_schema'))):
                raise ValueError('measurement belongs to another deployment')
            cached = prefix_pages(raw['leases'], row['resource'], prompt, resource['layout'], config['page_tokens'], start, as_of_us, compatibility=compatibility)
            if resource['role'] == 'prefill':
                coordinate = len(prompt)-cached
            elif resource['role'] == 'link':
                coordinate = len(prompt)*config['bytes_per_token']
            else:
                coordinate = len(prompt)
            if coordinate != integer(row['coordinate']):
                raise ValueError('declared coordinate is not evidenced')
            if cached != integer(row['cached_tokens']):
                raise ValueError('partial, stale or contradictory page proof')
            samples.append(dict(resource=row['resource'], sample_id=row['sample_id'], attempt=integer(row['attempt']),
                                coordinate=coordinate, service_us=float(end-start), event_us=float(end), cohort=row['cohort']))
            dispositions[source] = 'accepted'
        except (KeyError, ValueError, TypeError, ArithmeticError):
            dispositions[source] = 'excluded'
    surfaces, drift = {}, {}
    for identity, spec in config['resources'].items():
        owned = [s for s in samples if s['resource'] == identity]
        costs, support = [], []
        for coordinate in spec['axis']:
            values = [s['service_us'] for s in owned if s['cohort'] == 'baseline' and s['coordinate'] == coordinate]
            support.append(len(values))
            costs.append(float(median(values)) if len(values) >= config['minimum_samples'] else None)
        surfaces[identity] = dict(role=spec['role'], layout=spec['layout'], axis=spec['axis'], service_us=costs, support=support)
        recent = sorted([s for s in owned if s['cohort'] == 'recent'], key=lambda s: (s['event_us'], s['sample_id'], s['attempt']))[-config['drift_window']:]
        ratios = []
        for sample in recent:
            baseline = interpolate(surfaces[identity], sample['coordinate'])
            if baseline is not None and baseline > 0:
                ratios.append(sample['service_us']/baseline)
        drift[identity] = dict(state='insufficient_data', factor=None, samples=len(ratios))
        if len(ratios) == config['drift_window']:
            factor = float(median(ratios))
            drift[identity] = dict(state='drifting' if factor > config['drift_threshold'] else 'stable', factor=factor, samples=len(ratios))
    result = {'fabric-profiles': dict(as_of_us=as_of_us, resources=surfaces),
              'deployment-profile': dict(as_of_us=as_of_us, resources=deployment['resources'], ledger=deployment['rows']),
              'phase-drift': drift,
              'phase-ledger': dict(rows=[dict(source=s, disposition=v) for s,v in sorted(dispositions.items())], samples=sorted(samples,key=lambda s:(s['resource'],s['sample_id'],s['attempt']))),
              'phase-audit': dict(input_records=len(dispositions), qualified_samples=len(samples), dispositions=dict(sorted(Counter(dispositions.values()).items())))}
    constraints = compile_grammars(root, config, as_of_us)
    if constraints is not None:
        result['grammar-profile'] = constraints
    for name, value in result.items():
        write_json(out/(name+'.json'), value)
    return result


def interpolate(surface, coordinate):
    axis, costs = surface['axis'], surface['service_us']
    for x, cost in zip(axis, costs):
        if coordinate == x:
            return cost
    for i in range(len(axis)-1):
        if axis[i] < coordinate < axis[i+1]:
            left, right = costs[i:i+2]
            if left is None or right is None:
                return None
            return left+(right-left)*(coordinate-axis[i])/(axis[i+1]-axis[i])
    return None

"""Compatibility importer used by the colocated notebook and the PD pilot."""
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from serving_lab.storage import read_json, records, write_json
from .deployment import resolve_deployments


def compile_fabric(input_dir, output_dir, as_of_us):
    root, out = Path(input_dir), Path(output_dir)
    config = read_json(root/'fabric.json')
    exports = read_json(root/'phase-exports.json')
    latest = {}
    ledger = {}
    for source,row in records(root,exports['measurements']):
        if not row:
            continue
        try:
            if float(row['ingested_us']) > as_of_us:
                ledger[source] = 'deferred'
                continue
            duration = float(row['duration'])
            if row['status'] != 'ok' or duration <= 0 or duration > 10000:
                ledger[source] = 'excluded'
                continue
            latest[row['sample_id']] = row
            ledger[source] = 'accepted'
        except (ValueError, KeyError, TypeError):
            ledger[source] = 'excluded'
    samples=[]
    for row in latest.values():
        samples.append(dict(resource=row['resource'],sample_id=row['sample_id'],attempt=int(row['attempt']),coordinate=int(row['coordinate']),
                            service_us=float(row['duration']),event_us=float(row['ingested_us']),cohort=row['cohort']))
    pools=defaultdict(list)
    for sample in samples:
        pools[sample['resource']].append(sample['service_us'])
    profiles={}; drift={}
    for resource,spec in config['resources'].items():
        values=pools[resource]
        if values:
            cost=median(values)
            profiles[resource]=dict(role=spec['role'],layout=spec['layout'],axis=spec['axis'],service_us=[cost]*len(spec['axis']),support=[len(values)]*len(spec['axis']))
            drift[resource]=dict(state='stable',factor=1,samples=len(values))
    result={'fabric-profiles':dict(as_of_us=as_of_us,resources=profiles),'phase-ledger':dict(rows=[dict(source=s,disposition=v) for s,v in sorted(ledger.items())],samples=samples),
            'phase-drift':drift,'phase-audit':dict(input_records=len(ledger),qualified_samples=len(samples),dispositions=dict(Counter(ledger.values())))}
    deployment=resolve_deployments(root,exports,config,as_of_us)
    result['deployment-profile']=dict(as_of_us=as_of_us,resources=deployment['resources'],ledger=deployment['rows'])
    from .constraint_pilot import preview
    constraints=preview(root,config,as_of_us)
    if constraints is not None: result['grammar-profile']=constraints
    for name,value in result.items(): write_json(out/(name+'.json'),value)
    return result


def interpolate(surface, coordinate):
    axis=surface['axis']; costs=surface['service_us']
    index=min(range(len(axis)),key=lambda i:abs(axis[i]-coordinate))
    return costs[index]

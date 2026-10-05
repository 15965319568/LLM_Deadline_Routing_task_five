"""Build reproducible admission artifacts from an as-of export snapshot."""
from collections import Counter
from pathlib import Path
from .storage import catalogue, read_json, read_yaml, write_json
from .reconcile import available_groups, service_evidence
from .samples import claims_at, qualify
from .profiles import construct
from .drift import evaluate


def build_profiles(input_dir, output_dir, as_of_us):
    root, out = Path(input_dir), Path(output_dir)
    deployment = read_yaml(root / 'deployment.yaml')
    exports = read_json(root / 'exports.json')
    ledger, winners = available_groups(root, exports, catalogue(root), as_of_us)
    samples = qualify(winners, service_evidence(root, exports, as_of_us),
                      claims_at(root, exports, as_of_us), deployment, as_of_us, ledger)
    profiles = construct(deployment, samples, as_of_us)
    artifacts = {'profiles': profiles, 'drift': evaluate(deployment, profiles, samples),
                 'sample-ledger': {'rows': [{'source':k, 'disposition':v} for k,v in sorted(ledger.items())], 'samples':samples},
                 'audit': {'as_of_us':as_of_us, 'input_records':len(ledger),
                           'dispositions':{k:Counter(ledger.values())[k] for k in ('accepted','excluded','duplicate','deferred','conflict')},
                           'qualified_samples':len(samples)}}
    for name, value in artifacts.items():
        write_json(out / (name + '.json'), value)
    return artifacts

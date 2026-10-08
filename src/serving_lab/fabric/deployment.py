"""Registry-only importer retained by the first mixed-revision deployment pilot."""
from serving_lab.storage import records


def resolve_deployments(root, manifest, config, as_of_us):
    resources, ledger = {}, []
    for source, row in records(root, manifest.get('deployments', [])):
        if not row:
            continue
        if row.get('source_kind') == 'registry' and int(row.get('ingested_us', 0)) <= as_of_us:
            resource = row['resource']
            resources[resource] = {key: row.get(key) for key in ('resource','deployment_id','generation','config_hash','model_revision','tokenizer_revision','quantization','kv_schema','adapter','effective_us')}
            ledger.append(dict(source=source, disposition='candidate'))
    return dict(resources=resources, rows=ledger)

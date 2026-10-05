"""Import successful executions for the capacity notebook."""
from .schema import encode, integer
from .storage import records


def claims_at(root, exports, cutoff):
    cache = {}
    for _, row in records(root, exports['cache_claims']):
        if isinstance(row, dict) and 'claim_id' in row:
            cache[row['claim_id']] = row
    return cache


def qualify(winners, spans, claims, deployment, cutoff, ledger):
    rows = []
    by_model = {}
    for target in deployment['targets']:
        by_model.setdefault(target['model'], []).append(target)
    for source, item, run in winners:
        if item['status'] != 'ok' or item['ttft_us'] is None:
            continue
        try:
            trace = spans.get(item['span_id'])
            tokens = encode(run['tokenizer'].strip(), item['prompt'])
            claim = claims.get(trace['claim']) if trace else None
            matched = integer(claim['matched_tokens']) if claim else 0
            service = trace['service_us'] if trace else item['ttft_us']
            ended = trace['end_us'] if trace else item['available_us']
            candidates = by_model.get(run['model'].strip(), [])
            if not candidates:
                continue
            ledger[source] = 'accepted'
            for target in candidates:
                rows.append(dict(identity=list(item['key']), endpoint_id=target['endpoint_id'],
                    purpose=run['purpose'].strip(), tokens=max(0,len(tokens)-matched),
                    decodes=trace['decodes'] if trace else 0, service_us=float(service),
                    ttft_us=float(item['ttft_us']), ended_us=float(ended), available_us=item['available_us']))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return sorted(rows, key=lambda row: (row['endpoint_id'], row['identity']))

"""Join client attempts to service work and cache provenance."""
from collections import defaultdict
from .schema import encode, fingerprint, integer
from .storage import records


def claims_at(root, exports, cutoff):
    grouped, invalid = defaultdict(list), set()
    for _, raw in records(root, exports['cache_claims']):
        if raw is None:
            continue
        cid = raw.get('claim_id')
        try:
            if integer(raw['available_at_us']) > cutoff:
                continue
            row = dict(raw)
            for key in ('attempt', 'matched_tokens', 'created_at_us', 'expires_at_us', 'available_at_us'):
                row[key] = integer(row[key])
            if not isinstance(row['prefix'], list) or any(type(t) is not int for t in row['prefix']):
                raise ValueError('Invalid prefix')
            grouped[cid].append(row)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            invalid.add(cid)
    result = {}
    for cid, rows in grouped.items():
        payload = [{k: v for k, v in r.items() if k != 'available_at_us'} for r in rows]
        if cid not in invalid and all(r == payload[0] for r in payload):
            result[cid] = dict(rows[0], available_at_us=min(r['available_at_us'] for r in rows))
    return result


def qualify(winners, spans, claims, deployment, cutoff, ledger):
    targets = deployment['targets']
    accepted = []
    for source, item, run in winners:
        try:
            if run['phase'].strip() != 'measurement' or run['purpose'].strip() not in ('baseline', 'recent'):
                continue
            if item['status'] != 'ok' or item['ttft_us'] is None:
                continue
            trace = spans[item['span_id']]
            if trace['key'] != item['key'] or trace['end_us'] > cutoff:
                continue
            if trace['service_us'] > item['ttft_us']:
                continue
            endpoints = [p for p in targets if fingerprint(p) == fingerprint(run)]
            if not endpoints:
                continue
            tokens = encode(run['tokenizer'].strip(), item['prompt'])
            matched, cache_available = 0, 0
            if trace['claim'] is not None:
                claim = claims[trace['claim']]
                if (claim['run_id'], claim['request_id'], claim['attempt']) != item['key']:
                    continue
                if (claim['revision'], claim['tokenizer']) != (run['revision'].strip(), run['tokenizer'].strip()):
                    continue
                if not claim['created_at_us'] <= trace['start_us'] <= claim['expires_at_us']:
                    continue
                if not claim['prefix'] or tokens[:len(claim['prefix'])] != claim['prefix']:
                    continue
                if not 0 <= claim['matched_tokens'] <= len(claim['prefix']) <= len(tokens):
                    continue
                matched, cache_available = claim['matched_tokens'], claim['available_at_us']
            available = max(item['available_us'], integer(run['available_at_us']), trace['available_us'], cache_available)
            if available > cutoff:
                continue
            ledger[source] = 'accepted'
            for target in endpoints:
                accepted.append(dict(identity=list(item['key']), endpoint_id=target['endpoint_id'],
                    purpose=run['purpose'].strip(), tokens=len(tokens)-matched, decodes=trace['decodes'],
                    service_us=float(trace['service_us']), ttft_us=float(item['ttft_us']),
                    ended_us=float(trace['end_us']), available_us=available))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return sorted(accepted, key=lambda r: (r['endpoint_id'], r['identity']))

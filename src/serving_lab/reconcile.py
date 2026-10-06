"""Reconcile repeated deliveries without merging separate executions."""
from collections import defaultdict
from .schema import integer, measurement, span
from .storage import records


def available_groups(root, exports, runs, cutoff):
    ledger, groups = {}, defaultdict(list)
    for source, raw in records(root, exports['measurements']):
        ledger[source] = 'excluded'
        if raw is None:
            continue
        run_id = str(raw.get('run_id', raw.get('run', ''))).strip()
        run = runs.get(run_id)
        if run is None:
            continue
        try:
            item = measurement(raw, run)
            if max(item['available_us'], integer(run['available_at_us'])) > cutoff:
                ledger[source] = 'deferred'
                continue
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        groups[item['key']].append((source, item))
    winners = []
    for key, entries in groups.items():
        payloads = [{k: v for k, v in item.items() if k != 'available_us'} for _, item in entries]
        if any(p != payloads[0] for p in payloads):
            for source, _ in entries:
                ledger[source] = 'conflict'
            continue
        entries.sort(key=lambda pair: pair[0])
        for source, _ in entries[1:]:
            ledger[source] = 'duplicate'
        source, item = entries[0]
        item['available_us'] = min(r['available_us'] for _, r in entries)
        winners.append((source, item, runs[key[0]]))
    return ledger, winners


def service_evidence(root, exports, cutoff):
    groups = defaultdict(list)
    bad = set()
    for _, raw in records(root, exports['spans']):
        if raw is None:
            continue
        sid = raw.get('span_id', raw.get('id'))
        try:
            arrival = integer(raw.get('available_at_us', raw.get('delivered_us')))
            if arrival > cutoff:
                continue
            groups[sid].append(span(raw))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            bad.add(sid)
    result = {}
    for sid, entries in groups.items():
        payloads = [{k: v for k, v in entry.items() if k != 'available_us'} for entry in entries]
        if sid in bad or any(p != payloads[0] for p in payloads):
            continue
        result[sid] = dict(entries[0], available_us=min(e['available_us'] for e in entries))
    return result

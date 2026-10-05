"""Client delivery consolidation used by the capacity import job."""
from collections import defaultdict
from .schema import measurement, span
from .storage import records


def available_groups(root, exports, runs, cutoff):
    ledger, attempts = {}, defaultdict(list)
    for source, raw in records(root, exports['measurements']):
        ledger[source] = 'excluded'
        if not raw:
            continue
        run_id = str(raw.get('run_id', raw.get('run', ''))).strip()
        if run_id not in runs:
            continue
        try:
            item = measurement(raw, runs[run_id])
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        attempts[item['key'][:2]].append((source, item))
    winners = []
    for key, rows in attempts.items():
        rows.sort(key=lambda r: (r[1]['status'] == 'ok', r[1]['available_us'], r[0]))
        for source, _ in rows[:-1]:
            ledger[source] = 'duplicate'
        source, item = rows[-1]
        winners.append((source, item, runs[key[0]]))
    return ledger, winners


def service_evidence(root, exports, cutoff):
    index = {}
    for _, raw in records(root, exports['spans']):
        try:
            row = span(raw)
            previous = index.get(row['span_id'])
            if previous is None or row['available_us'] >= previous['available_us']:
                index[row['span_id']] = row
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    return index

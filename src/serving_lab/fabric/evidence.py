"""Causal producer-clock evidence, attempt reconciliation and page provenance."""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from serving_lab.storage import records, read_json
from .policy import valid_lease


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a measurement')
    result = Decimal(str(value).strip())
    if not result.is_finite() or result < 0:
        raise ValueError('nonnegative finite number required')
    return result


def integer(value):
    result = number(value)
    if result != int(result):
        raise ValueError('integer required')
    return int(result)


def measurement_identity(row):
    if any(not isinstance(row.get(key), str) or not row[key] for key in ('resource', 'sample_id')):
        raise ValueError('measurement identity requires nonempty resource and sample_id')
    return row['resource'], row['sample_id'], integer(row['attempt'])


def grouped(rows, key, as_of):
    """Invisible copies cannot invalidate an already available identity."""
    groups = defaultdict(list)
    for source, row in rows:
        try:
            if isinstance(row, dict) and integer(row['ingested_us']) <= as_of:
                groups[key(row)].append((source, row))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    good, states = {}, {}
    for identity, copies in groups.items():
        signatures = {tuple(sorted((k, str(v)) for k, v in row.items() if k != 'ingested_us')) for _, row in copies}
        if len(signatures) != 1:
            states.update((source, 'conflict') for source, _ in copies)
        else:
            winner = min(copies, key=lambda pair: pair[0])
            good[identity] = winner[1]
            states.update((source, 'candidate' if source == winner[0] else 'duplicate') for source, _ in copies)
    return good, states


def clock_time(anchors, producer, boot, tick, as_of):
    unique, states = grouped(anchors, lambda r: (r['producer'], r['boot'], integer(r['tick'])), as_of)
    conflicts = [integer(r['tick']) for source,r in anchors if states.get(source) == 'conflict'
                 and r['producer'] == producer and r['boot'] == boot]
    points = sorted((integer(r['tick']), number(r['global_us'])) for r in unique.values()
                    if r['producer'] == producer and r['boot'] == boot and number(r['global_us']) <= as_of)
    tick = number(tick)
    for (left, start), (right, end) in zip(points, points[1:]):
        if left <= tick <= right and right > left and end > start and not any(left <= bad <= right for bad in conflicts):
            return start + (end-start) * (tick-left) / (right-left)
    raise ValueError('no consistent bracket in this boot')


def prefix_pages(rows, resource, prompt, layout, page_tokens, now, as_of=None, session_id=None, compatibility=None):
    """Lease intervals are half-open; an unproven gap terminates the prefix."""
    as_of = now if as_of is None else as_of
    # A refresh may carry a revocation beside an older lease. The tombstone
    # wins for that lease id without turning the refresh into a duplicate
    # conflict, which is important when cache sources arrive out of order.
    revoked_ids, candidates = set(), []
    for source, row in rows:
        if not isinstance(row, dict):
            continue
        if 'revoked_us' not in row:
            candidates.append((source, row))
            continue
        try:
            clean = {k: v for k, v in row.items() if k != 'revoked_us'}
            revoked, start = row['revoked_us'], row['valid_from_us']
            if (isinstance(revoked, bool) or not isinstance(revoked, int) or revoked <= start or
                    valid_lease(clean, start, as_of, resource, layout, page_tokens, session_id, compatibility) is None):
                continue
            candidates.append((source, clean))
            if revoked <= now:
                revoked_ids.add(row['lease_id'])
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    unique, _ = grouped(candidates, lambda r: r['lease_id'], as_of)
    pages = defaultdict(set)
    for lease_id, row in unique.items():
        if lease_id in revoked_ids:
            continue
        proven = valid_lease(row, now, as_of, resource, layout, page_tokens, session_id, compatibility)
        if proven is not None:
            index, tokens = proven
            pages[index].add(tokens)
    count = 0
    while (count+1)*page_tokens <= len(prompt):
        expected = prompt[count*page_tokens:(count+1)*page_tokens]
        if pages[count] != {expected}:
            break
        count += 1
    return count * page_tokens


def sources(root, manifest):
    result = {}
    for kind in ['measurements', 'anchors', 'traces', 'leases', 'deployments']:
        result[kind] = []
        result[kind].extend(records(root, manifest.get(kind, [])))
    return result

"""Helpers for the original single-producer experiment, used by notebooks.

This compatibility module is intentionally incomplete: the pilot still trusts
the producer's declared values and therefore needs the V6 evidence contract.
"""
from collections import defaultdict
from decimal import Decimal


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not numeric')
    result = Decimal(str(value).strip())
    if not result.is_finite() or result < 0:
        raise ValueError('nonnegative finite number required')
    return result


def integer(value):
    result = number(value)
    if result != int(result):
        raise ValueError('integer required')
    return int(result)


def prefix_pages(leases, resource, prompt, layout, page_tokens, now, session_id=None):
    """Legacy implementation; it deliberately omits V6 page lineage checks."""
    pages = defaultdict(set)
    for row in leases:
        try:
            if (row['resource'] == resource and row['layout'] == layout and
                    row.get('session_id', '') == (session_id if session_id else '') and
                    integer(row['valid_from_us']) <= now < integer(row['expires_us'])):
                pages[integer(row['page_index'])].add(row['tokens'])
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    count = 0
    while (count + 1) * page_tokens <= len(prompt) and pages[count] == {prompt[count*page_tokens:(count+1)*page_tokens]}:
        count += 1
    return count * page_tokens


def span_duration(span):
    return float(span['end_tick'])-float(span['start_tick'])


def cached_tokens(leases, resource):
    return sum(len(row['tokens']) for row in leases if row['resource']==resource)

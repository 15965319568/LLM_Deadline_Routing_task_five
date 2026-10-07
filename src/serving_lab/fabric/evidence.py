"""Helpers for the original single-producer experiment, used by notebooks."""
def span_duration(span):
    return float(span['end_tick'])-float(span['start_tick'])


def cached_tokens(leases, resource):
    return sum(len(row['tokens']) for row in leases if row['resource']==resource)

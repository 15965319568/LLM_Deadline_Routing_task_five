"""Small policy primitives shared by offline evidence and the live gateway.

The serving path must not infer tenant, session, or cache identity from a
request id.  Keeping the validation and wire checks here also makes the
policy testable without starting an ASGI application.
"""
import hashlib
import json
from collections import deque


def request_fields(body, config, now):
    if not isinstance(body, dict) or body.get('model') != config['model'] or body.get('stream') is not True:
        raise ValueError('unsupported body')
    prompt = body.get('prompt')
    if not isinstance(prompt, str):
        raise ValueError('prompt must be unicode text')
    output = body['max_tokens']
    deadline = body['deadline_us']
    if isinstance(output, bool) or not isinstance(output, int) or output < 1:
        raise ValueError('invalid output budget')
    if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < now:
        raise ValueError('invalid deadline')
    tenant = body.get('tenant', config.get('default_tenant', 'default'))
    limits = config.get('tenant_limits', {})
    if not isinstance(tenant, str) or not tenant or (limits and tenant not in limits):
        raise ValueError('unknown tenant')
    priority = body.get('priority', 0)
    maximum = int(limits.get(tenant, {}).get('max_priority', 3))
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0 or priority > maximum:
        raise ValueError('invalid priority')
    session = body.get('session_id')
    if session is not None and (not isinstance(session, str) or not session or len(session) > 64):
        raise ValueError('invalid session')
    return prompt, output, deadline, tenant, priority, session


def priority_factor(config, priority):
    factors = config.get('priority_factors', [1.0])
    if priority < 0 or priority >= len(factors):
        raise ValueError('missing priority factor')
    value = float(factors[priority])
    if value <= 0:
        raise ValueError('invalid priority factor')
    return value


def start_allowed(log, tenant, now, config):
    """Return whether the tenant may start one more request in its rolling window."""
    quota = config.get('tenant_limits', {}).get(tenant, {})
    limit = int(quota.get('max_starts', 0))
    window = int(quota.get('start_window_us', 0))
    if limit <= 0 or window <= 0:
        return True
    recent = log.setdefault(tenant, deque())
    while recent and now - recent[0] >= window:
        recent.popleft()
    return len(recent) < limit


def record_start(log, tenant, now):
    log.setdefault(tenant, deque()).append(now)


def page_hash(layout, namespace, page_index, tokens):
    material = f'{layout}|{namespace}|{page_index}|{tokens}'.encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def valid_lease(row, now, as_of, resource, layout, page_tokens, session_id):
    try:
        if not isinstance(row, dict) or row.get('resource') != resource or row.get('layout') != layout:
            return None
        producer = row['producer']
        generation = row['generation']
        namespace = row.get('session_id', '')
        index = row['page_index']
        tokens = row['tokens']
        start, expires, ingested = row['valid_from_us'], row['expires_us'], row['ingested_us']
        if not isinstance(producer, str) or not producer or isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            return None
        if not isinstance(namespace, str) or not isinstance(tokens, str) or len(tokens) != page_tokens:
            return None
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in [start, expires, ingested]):
            return None
        if expires <= start or ingested > as_of or not (start <= now < expires):
            return None
        if namespace != session_id if session_id else namespace != '':
            return None
        if row.get('page_hash') != page_hash(layout, namespace, index, tokens):
            return None
        return index, tokens
    except (KeyError, TypeError, ValueError):
        return None


class StreamTracker:
    """Parse SSE data across arbitrary chunk boundaries."""
    def __init__(self):
        self._buffer = ''
        self.first = False
        self.done = False

    def feed(self, chunk):
        if isinstance(chunk, bytes):
            chunk = chunk.decode('utf-8', errors='replace')
        self._buffer += chunk
        lines = self._buffer.split('\n')
        self._buffer = lines.pop()
        for line in lines:
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                self.done = True
                continue
            try:
                document = json.loads(payload)
                choices = document.get('choices') or []
                text = choices[0].get('text', '') if choices else ''
                if isinstance(text, str) and text:
                    self.first = True
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
        return self.first

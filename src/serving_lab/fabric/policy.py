"""Small policy primitives shared by offline evidence and the live gateway."""
import hashlib
import json
import codecs
from collections import deque


def request_fields(body, config, now):
    if not isinstance(body, dict) or body.get('model') != config['model'] or body.get('stream') is not True:
        raise ValueError('unsupported body')
    prompt = body.get('prompt')
    if not isinstance(prompt, str):
        raise ValueError('prompt must be unicode text')
    output, deadline = body['max_tokens'], body['deadline_us']
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
    quota = config.get('tenant_limits', {}).get(tenant, {})
    limit, window = int(quota.get('max_starts', 0)), int(quota.get('start_window_us', 0))
    if limit <= 0 or window <= 0:
        return True
    recent = log.setdefault(tenant, deque())
    while recent and now - recent[0] >= window:
        recent.popleft()
    return len(recent) < limit


def record_start(log, tenant, now):
    log.setdefault(tenant, deque()).append(now)


def page_hash(layout, namespace, page_index, tokens):
    return hashlib.sha256(f'{layout}|{namespace}|{page_index}|{tokens}'.encode('utf-8')).hexdigest()


def valid_lease(row, now, as_of, resource, layout, page_tokens, session_id):
    try:
        if row.get('resource') != resource or row.get('layout') != layout:
            return None
        namespace, index, tokens = row.get('session_id', ''), row['page_index'], row['tokens']
        start, expires, ingested = row['valid_from_us'], row['expires_us'], row['ingested_us']
        if not isinstance(row['producer'], str) or not row['producer']:
            return None
        if isinstance(row['generation'], bool) or not isinstance(row['generation'], int) or row['generation'] < 0:
            return None
        if not isinstance(namespace, str) or not isinstance(tokens, str) or len(tokens) != page_tokens:
            return None
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in [start, expires, ingested]):
            return None
        if expires <= start or ingested > as_of or not (start <= now < expires):
            return None
        if (namespace != session_id) if session_id else (namespace != ''):
            return None
        if row.get('page_hash') != page_hash(layout, namespace, index, tokens):
            return None
        return index, tokens
    except (KeyError, TypeError, ValueError):
        return None


class StreamTracker:
    def __init__(self):
        self._buffer, self.first, self.done, self.invalid = '', False, False, False
        self._data = []
        self._decoder = codecs.getincrementaldecoder('utf-8')('strict')

    def feed(self, chunk):
        try:
            self._buffer += self._decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        except UnicodeDecodeError:
            self.invalid = True
            return self.first
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            line = line.removesuffix('\r')
            if line:
                if line.startswith('data:'):
                    self._data.append(line[5:].removeprefix(' '))
                continue
            if not self._data:
                continue
            payload, self._data = '\n'.join(self._data), []
            if self.done:
                self.invalid = True
                continue
            if payload == '[DONE]':
                self.done = True
                if not self.first:
                    self.invalid = True
                continue
            try:
                choices = json.loads(payload).get('choices') or []
                text = choices[0].get('text', '') if choices else ''
                if isinstance(text, str) and text and not self.invalid:
                    self.first = True
            except (TypeError, ValueError, IndexError, AttributeError):
                self.invalid = True
        return self.first

    def complete(self):
        try:
            self._decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            self.invalid = True
        return self.first and self.done and not self.invalid and not self._data and not self._buffer.strip()

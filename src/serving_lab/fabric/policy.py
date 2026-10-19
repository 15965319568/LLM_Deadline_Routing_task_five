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
    if 'session_id' in body and (not isinstance(session, str) or not session or len(session) > 64):
        raise ValueError('invalid session')
    contract = config.get('deployment_contract', {})
    compatibility = {}
    for field in ('model_revision','tokenizer_revision','quantization','adapter'):
        value = body.get(field, contract.get(field))
        if not isinstance(value, str) or not value:
            raise ValueError('invalid deployment selector')
        compatibility[field] = value
    kv_schema = body.get('kv_schema')
    if 'kv_schema' in body and (not isinstance(kv_schema, str) or not kv_schema):
        raise ValueError('invalid kv schema')
    compatibility['kv_schema'] = kv_schema
    compatibility['explicit'] = any(field in body for field in compatibility if field != 'explicit')
    return prompt, output, deadline, tenant, priority, session, compatibility


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


def valid_lease(row, now, as_of, resource, layout, page_tokens, session_id, compatibility=None):
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
        revoked = row.get('revoked_us')
        if revoked is not None:
            if isinstance(revoked, bool) or not isinstance(revoked, int) or revoked <= start:
                return None
            if now >= revoked:
                return None
        if expires <= start or ingested > as_of or not (start <= now < expires):
            return None
        if (namespace != session_id) if session_id else (namespace != ''):
            return None
        if row.get('page_hash') != page_hash(layout, namespace, index, tokens):
            return None
        if compatibility:
            for field in ('model_revision','tokenizer_revision','quantization','kv_schema','adapter','deployment_id','generation'):
                if field in ('deployment_id','generation') and 'deployment_id' not in row:
                    continue
                if compatibility.get(field) is not None and row.get(field, compatibility.get(field) if 'deployment_id' not in row else None) != compatibility.get(field):
                    return None
        return index, tokens
    except (KeyError, TypeError, ValueError):
        return None


class StreamTracker:
    def __init__(self):
        self._buffer, self.first, self.done, self.invalid = '', False, False, False
        self._decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self._data = []

    def _line(self, line):
        line = line.removesuffix('\r')
        if line == '':
            if self._data:
                payload = '\n'.join(self._data)
                self._data.clear()
                if payload == '[DONE]':
                    self.done = True
                else:
                    try:
                        choices = json.loads(payload).get('choices') or []
                        text = choices[0].get('text', '') if choices else ''
                        if isinstance(text, str) and text:
                            self.first = True
                    except (TypeError, ValueError, IndexError, AttributeError):
                        self.invalid = True
            return
        if line.startswith(':'):
            return
        if line and not line.strip():
            return
        if self.done:
            self.invalid = True
            return
        field, separator, value = line.partition(':')
        if field == 'data':
            self._data.append(value.removeprefix(' ') if separator else '')

    def feed(self, chunk):
        try:
            self._buffer += self._decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        except UnicodeDecodeError:
            self.invalid = True
            return self.first
        lines = self._buffer.split('\n')
        self._buffer = lines.pop()
        for line in lines:
            self._line(line)
        return self.first

    def close(self):
        try:
            self._buffer += self._decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            self.invalid = True
        if self._data or (self._buffer.strip() and not self._buffer.startswith(':')):
            self.invalid = True

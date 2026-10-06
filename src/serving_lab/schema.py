"""Wire format normalization; durations remain decimal until output."""
from decimal import Decimal

FINGERPRINT = ('model', 'revision', 'tokenizer', 'accelerator', 'parallelism', 'quantization')


def number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean measurement')
    result = Decimal(str(value).strip())
    if not result.is_finite() or result < 0:
        raise ValueError('Non-finite or negative measurement')
    return result


def integer(value):
    result = number(value)
    if result != result.to_integral_value():
        raise ValueError('Fractional integer')
    return int(result)


def fingerprint(row):
    return tuple(str(row[k]).strip() for k in FINGERPRINT)


def encode(tokenizer, text):
    if not isinstance(text, str):
        raise ValueError('Prompt must be text')
    if tokenizer == 'codepoint-v1':
        return list(map(ord, text))
    if tokenizer == 'utf8-v1':
        return list(text.encode('utf-8'))
    raise ValueError('Unknown tokenizer')


def measurement(row, run):
    version = run['schema'].strip()
    if version == 'bench/1':
        names = ('run', 'request', 'attempt', 'prompt', 'span', 'outcome', 'ttft', 'delivered_us')
        scale = Decimal(1_000_000)
    elif version == 'bench/2':
        names = ('run_id', 'request_id', 'attempt', 'prompt', 'span_id', 'status', 'client_ttft', 'available_at_us')
        scale = Decimal(1)
    else:
        raise ValueError('Unsupported benchmark schema')
    rid, request, attempt, prompt, sid, status, ttft, available = (row[n] for n in names)
    return dict(key=(rid.strip(), request.strip(), integer(attempt)), prompt=prompt,
                span_id=sid.strip(), status=status.strip().lower(),
                ttft_us=None if ttft in ('', None) else number(ttft) * scale,
                available_us=integer(available))


def span(row):
    if row['schema'] == 'span/1':
        keys = ('id', 'run', 'request', 'try', 'begin_ms', 'end_ms', 'decoding', 'cached', 'delivered_us')
        scale = Decimal(1000)
    elif row['schema'] == 'span/2':
        keys = ('span_id', 'run_id', 'request_id', 'attempt', 'prefill_start_us', 'prefill_end_us',
                'active_decodes', 'cache_claim_id', 'available_at_us')
        scale = Decimal(1)
    else:
        raise ValueError('Unsupported span schema')
    sid, run, request, attempt, start, end, decodes, claim, available = (row[k] for k in keys)
    begin, finish = number(start) * scale, number(end) * scale
    if finish < begin or row['complete'] is not True:
        raise ValueError('Incomplete service span')
    return dict(span_id=sid, key=(run, request, integer(attempt)), start_us=begin, end_us=finish,
                service_us=finish-begin, decodes=integer(decodes), claim=claim or None,
                available_us=integer(available))

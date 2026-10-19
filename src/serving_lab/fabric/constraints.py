"""Versioned byte grammars and committed decoding state."""
import copy
import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from serving_lab.storage import read_json


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('invalid grammar integer')
    return value


def grammar(value):
    states = value['states']
    if not isinstance(states, list) or not states or len(set(states)) != len(states):
        raise ValueError('invalid grammar states')
    for state in states: integer(state)
    if integer(value['start']) not in states or not isinstance(value['accept'], list):
        raise ValueError('invalid grammar start/accept')
    if any(integer(s) not in states for s in value['accept']):
        raise ValueError('unknown accepting state')
    if not isinstance(value['tokenizer_revision'], str) or not value['tokenizer_revision']:
        raise ValueError('invalid grammar tokenizer')
    integer(value['revision'], 1)
    arcs = {}
    for edge in value['edges']:
        source, byte, target = (integer(edge[k]) for k in ['from', 'byte', 'to'])
        if source not in states or target not in states or byte > 255:
            raise ValueError('invalid grammar transition')
        if (source, byte) in arcs and arcs[source, byte] != target:
            raise ValueError('nondeterministic grammar')
        arcs[source, byte] = target
    return dict(revision=value['revision'], tokenizer_revision=value['tokenizer_revision'],
                states=sorted(states), start=value['start'], accept=sorted(set(value['accept'])),
                edges=[dict(zip(['from','byte','to'],[s,b,t])) for (s,b),t in sorted(arcs.items())])


def compile_grammars(root, config, cutoff):
    root = Path(root)
    if not config.get('grammar_exports'): return None
    manifest = read_json(root/config['grammar_exports'])
    groups, ledger = defaultdict(list), {}
    for entry in manifest['sources']:
        path = root/entry['path']
        opener = gzip.open if entry['format'] == 'jsonl.gz' else open
        with opener(path, 'rt', encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream)) if entry['format'] == 'csv' else [json.loads(line) for line in stream if line.strip()]
        for index, raw in enumerate(rows, 1):
            source = entry['path']+'#'+str(index)
            try:
                row = dict(raw)
                if entry['format'] == 'csv':
                    for field in ['revision','event_us','ingested_us']: row[field] = int(row[field])
                    row['payload'] = json.loads(row['payload'])
                gid = row['grammar_id']
                if not isinstance(gid, str) or not gid or row['part'] not in ['metadata','arcs','release']:
                    raise ValueError('invalid grammar identity')
                revision = integer(row['revision'], 1)
                event, ingested = integer(row['event_us']), integer(row['ingested_us'])
                if not isinstance(row['payload'], (dict, list)): raise ValueError('invalid payload')
                if event > cutoff or ingested > cutoff:
                    ledger[source] = 'deferred'; continue
                semantic = dict(payload=row['payload'], event_us=event)
                groups[gid,revision,row['part']].append((source, semantic))
            except (KeyError,ValueError,TypeError): ledger[source] = 'invalid'
    pieces = {}
    for key, rows in groups.items():
        if any(row[1] != rows[0][1] for row in rows):
            for source,_ in rows: ledger[source] = 'conflict'
        else:
            source, value = min(rows)
            pieces[key] = (source, value['payload'])
            for other,_ in rows: ledger[other] = 'duplicate' if other != source else 'excluded'
    valid, selected = {}, {}
    for gid, revision in sorted({key[:2] for key in groups}):
        keys = [(gid,revision,part) for part in ['metadata','arcs','release']]
        if any(key not in pieces for key in keys): continue
        meta, delta, release = (pieces[key][1] for key in keys)
        try:
            if release['enabled'] is not True or not isinstance(delta,list): continue
            parent = meta.get('base_revision')
            edges = {}
            if parent is not None:
                integer(parent,1)
                if parent >= revision: raise ValueError('cyclic grammar lineage')
                base = valid[gid,parent]
                if base['tokenizer_revision'] != meta['tokenizer_revision']: raise ValueError('cross tokenizer base')
                edges = {(r['from'],r['byte']):r['to'] for r in base['edges']}
            for edge in delta:
                source, byte, target = (integer(edge[k]) for k in ['from','byte','to'])
                key = source,byte
                if edge['op'] == 'remove':
                    if edges.get(key) != target: raise ValueError('unproven grammar removal')
                    del edges[key]
                elif edge['op'] == 'add':
                    if key in edges and edges[key] != target: raise ValueError('conflicting grammar patch')
                    edges[key] = target
                else: raise ValueError('invalid grammar operation')
            value = grammar(dict(meta, revision=revision, edges=[dict(zip(['from','byte','to'],[s,b,t])) for (s,b),t in edges.items()]))
            valid[gid,revision] = value; selected[gid] = value
            for key in keys: ledger[pieces[key][0]] = 'shadowed'
        except (KeyError,TypeError,ValueError): pass
    for gid, row in selected.items():
        for part in ['metadata','arcs','release']: ledger[pieces[gid,row['revision'],part][0]] = 'selected'
    return dict(as_of_us=cutoff,grammars=selected,ledger=[dict(source=s,disposition=d) for s,d in sorted(ledger.items())])


def load_grammars(directory, config, now):
    if not config.get('grammar_exports'): return {}
    document = read_json(Path(directory)/'grammar-profile.json')
    if integer(document['as_of_us']) > now: raise ValueError('future grammar profile')
    return {key:grammar(value) for key,value in document['grammars'].items()}


def options(body):
    result = {}
    for key, default, low, high in [('frequency_penalty',0,-2,2),('presence_penalty',0,-2,2),('repetition_penalty',1,0,10)]:
        value = body.get(key,default)
        if type(value) not in (float,int) or not math.isfinite(value) or not low <= value <= high or (key=='repetition_penalty' and value==0):
            raise ValueError('invalid history penalty')
        result[key] = value
    history = body.get('prompt_token_ids',[])
    if not isinstance(history,list): raise ValueError('invalid token history')
    result['prompt_token_ids'] = [integer(t) for t in history]
    result['min_tokens'] = integer(body.get('min_tokens',0))
    if result['min_tokens'] > body['max_tokens']: raise ValueError('minimum exceeds budget')
    gid = body.get('grammar')
    if 'grammar' in body and (not isinstance(gid,str) or not gid): raise ValueError('invalid grammar selector')
    result['grammar'] = gid
    stops = body.get('stop',[])
    if not isinstance(stops,list) or len(stops)>4 or any(not isinstance(s,str) or not s or len(s.encode('utf-8'))>64 for s in stops):
        raise ValueError('invalid stop sequences')
    result['stop'] = stops
    return result


class DecodeState:
    def __init__(self, asset, options, grammar=None):
        self.asset, self.options, self.grammar = asset, options, grammar
        self.counts = Counter(options.get('prompt_token_ids',[]))
        self.generated, self.pending, self.reason = 0, b'', None
        self.state = grammar['start'] if grammar else None
        self.arcs = {(r['from'],r['byte']):r['to'] for r in grammar['edges']} if grammar else {}
        self.stops = [s.encode('utf-8') for s in options.get('stop',[])]

    def next_state(self, token):
        if token == self.asset['eos_id']:
            if self.generated < self.options.get('min_tokens',0): return False,None
            return (self.grammar is None or self.state in self.grammar['accept']),self.state
        state = self.state
        if self.grammar:
            for byte in bytes.fromhex(self.asset['vocabulary'][token]):
                if (state,byte) not in self.arcs: return False,None
                state = self.arcs[state,byte]
        return True,state

    def adjust(self, values):
        result=[]; permitted=[]
        for token,value in enumerate(values):
            count = self.counts[token]
            penalty = self.options.get('repetition_penalty',1)
            if count: value = value/penalty if value>0 else value*penalty
            value -= self.options.get('presence_penalty',0)*(count>0)+self.options.get('frequency_penalty',0)*count
            result.append(value)
            if self.next_state(token)[0]: permitted.append(token)
        return result,permitted

    def append(self, token, limit):
        allowed,state = self.next_state(token)
        if not allowed: raise ValueError('sample violates grammar')
        self.state=state; self.counts[token]+=1; self.generated+=1
        self.pending+=bytes.fromhex(self.asset['vocabulary'][token])
        matches=[(self.pending.find(stop)+len(stop),len(stop),index) for index,stop in enumerate(self.stops) if stop in self.pending]
        if matches:
            end,length,_ = min(matches,key=lambda r:(r[0],-r[1],r[2]))
            text=self.pending[:end-length].decode('utf-8'); self.pending=b''; self.reason='stop'
            return text
        if token==self.asset['eos_id'] or self.generated>=limit:
            text=self.pending.decode('utf-8'); self.pending=b''
            self.reason='eos' if token==self.asset['eos_id'] else 'length'
            return text
        hold=max([n for stop in self.stops for n in range(1,min(len(stop),len(self.pending))+1) if self.pending.endswith(stop[:n])] or [0])
        safe=len(self.pending)-hold
        while safe:
            try: text=self.pending[:safe].decode('utf-8'); break
            except UnicodeDecodeError as exc:
                if exc.reason!='unexpected end of data': raise
                safe=exc.start
        else: text=''
        self.pending=self.pending[safe:]
        return text

    def report(self):
        return dict(grammar_id=self.options.get('grammar'),grammar_revision=self.grammar['revision'] if self.grammar else None,
                    state=self.state,generated_tokens=self.generated,history_counts={str(k):v for k,v in sorted(self.counts.items()) if v},
                    pending_bytes=len(self.pending),stop_reason=self.reason)

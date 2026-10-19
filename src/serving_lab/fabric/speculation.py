"""Decode quantized tensor-parallel windows against the admitted model snapshot."""
import codecs
import copy
import json
import math
from pathlib import Path
from serving_lab.storage import read_json
from .constraints import DecodeState, options as history_options
from .tree_decode import begin_tree, shard_tree
from .paged_attention import PageContext, validate_asset


def natural(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError('invalid integer')
    return value


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('invalid real')
    return float(value)


def sampling(body):
    mode = body.get('decode_mode')
    if 'decode_kernel' in body and (mode != 'speculative' or body['decode_kernel'] not in ['logits','paged-gqa']):
        raise ValueError('invalid decode kernel')
    if 'decode_mode' not in body:
        return None
    if mode != 'speculative':
        raise ValueError('unsupported decode mode')
    result = dict(temperature=finite(body.get('temperature', 1)), top_p=finite(body.get('top_p', 1)),
                  top_k=natural(body.get('top_k', 0)), logit_bias=body.get('logit_bias', {}))
    if result['temperature'] <= 0 or not 0 < result['top_p'] <= 1 or not isinstance(result['logit_bias'], dict):
        raise ValueError('invalid sampler')
    for key, value in result['logit_bias'].items():
        if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
            raise ValueError('invalid token id')
        if abs(finite(value)) > 100:
            raise ValueError('invalid logit bias')
    result['decode_kernel'] = body.get('decode_kernel','logits')
    result.update(history_options(body))
    return result


def assets(source, config, deployments):
    if not config.get('decode_assets'):
        return {}
    catalogue = read_json(Path(source)/config['decode_assets'])
    result = {}
    for resource, dep in deployments.items():
        if config['resources'][resource]['role'] != 'decode':
            continue
        item = copy.deepcopy(catalogue[dep['deployment_id']])
        natural(item['generation'])
        if any(item[key] != dep[key] for key in ('deployment_id', 'generation', 'tokenizer_revision', 'quantization')):
            raise ValueError('decode asset deployment mismatch')
        vocab, shards = item['vocabulary'], item['shards']
        if not isinstance(vocab, list) or len(vocab) < 2 or not isinstance(shards, list) or not shards:
            raise ValueError('empty decode assets')
        for token in vocab:
            if not isinstance(token, str):
                raise ValueError('invalid token bytes')
            bytes.fromhex(token)
        natural(item['eos_id'])
        if item['eos_id'] >= len(vocab) or vocab[item['eos_id']] != '':
            raise ValueError('invalid EOS')
        owned = []
        for shard in shards:
            if finite(shard['scale']) <= 0:
                raise ValueError('invalid quantizer scale')
            natural(shard['zero_point'])
            owned.extend(natural(x) for x in shard['token_ids'])
        if sorted(owned) != list(range(len(vocab))):
            raise ValueError('tensor partition must cover vocabulary once')
        result[resource] = {key:item[key] for key in ('deployment_id','generation','tokenizer_revision','quantization','eos_id','vocabulary')}
        result[resource]['shards'] = [{key:shard[key] for key in ('token_ids','scale','zero_point')} for shard in shards]
        if 'paged_attention' in item:
            result[resource]['paged_attention'] = validate_asset(item['paged_attention'],len(vocab))
    return result


def distribution(logits, options, state=None):
    count = len(logits)
    if any(int(key) >= count for key in options['logit_bias']):
        raise ValueError('bias token outside vocabulary')
    values = [finite(x)+options['logit_bias'].get(str(i), 0) for i,x in enumerate(logits)]
    order = sorted(range(count), key=lambda i: (-values[i], i))
    if state is not None:
        values, permitted = state.adjust(values)
        order = sorted(permitted, key=lambda i: (-values[i], i))
    if not order:
        raise ValueError('empty constrained distribution')
    keep = order[:options['top_k']] if options['top_k'] else order
    maximum = values[keep[0]]
    weights = {i: math.exp((values[i]-maximum)/options['temperature']) for i in keep}
    denominator = sum(weights.values())
    cumulative, nucleus = 0., []
    for i in keep:
        nucleus.append(i)
        cumulative += weights[i]/denominator
        if cumulative >= options['top_p']:
            break
    total = sum(weights[i] for i in nucleus)
    return [weights.get(i, 0)/total if i in nucleus else 0. for i in range(count)]


def draw(probabilities, uniform):
    uniform = finite(uniform)
    if not 0 <= uniform < 1:
        raise ValueError('uniform outside [0,1)')
    cumulative = 0.
    for index, probability in enumerate(probabilities):
        cumulative += probability
        if cumulative > uniform:
            return index
    return max(i for i,p in enumerate(probabilities) if p > 0)


class SpeculativeDecoder:
    def __init__(self, rid, asset, options, limit, reserve, release, allocate_pages=None, page_factory=None):
        self.rid, self.asset, self.options = rid, copy.deepcopy(asset), copy.deepcopy(options)
        self.limit, self.reserve, self.release = limit, reserve, release
        self.window, self.sequence, self.position, self.stopped = None, 0, 0, False
        self.wire = codecs.getincrementaldecoder('utf-8')()
        self.tokens = codecs.getincrementaldecoder('utf-8')()
        self.buffer, self.done = '', False
        self.stats = dict(proposed=0, accepted=0, corrected=0, committed=0, windows=0, scratch_pages=0)
        self.state = DecodeState(self.asset, self.options, self.options.get('grammar_asset'))
        self.decoding = self.state.report()
        self.paged = (page_factory(self.asset) if page_factory else PageContext(self.asset,allocate_pages)) if options.get('decode_kernel')=='paged-gqa' else None

    def feed(self, chunk):
        self.buffer = (self.buffer+self.wire.decode(chunk)).replace('\r\n', '\n')
        while '\n\n' in self.buffer:
            event, self.buffer = self.buffer.split('\n\n', 1)
            lines = event.split('\n')
            data = '\n'.join(line[5:].lstrip(' ') for line in lines if line.startswith('data:'))
            if not data:
                if any(line.strip() and not line.startswith(':') for line in lines):
                    raise ValueError('non-data protocol event')
                continue
            if self.done:
                raise ValueError('event after DONE')
            if data == '[DONE]':
                if self.window is not None or not self.stopped:
                    raise ValueError('unfinished speculative decode')
                self.tokens.decode(b'', final=True)
                self.done = True
                yield b'data: [DONE]\n\n'
                continue
            row = json.loads(data)
            if not isinstance(row, dict):
                raise ValueError('invalid tensor event')
            for key, expected in [('request_id', self.rid), ('deployment_id', self.asset['deployment_id']),
                                  ('generation', self.asset['generation']), ('tokenizer_revision', self.asset['tokenizer_revision'])]:
                if row.get(key) != expected:
                    raise ValueError('tensor event belongs to another admitted deployment')
            if natural(row.get('window')) != self.sequence:
                raise ValueError('window sequence mismatch')
            kind = row.get('kind')
            if kind == 'begin':
                self.begin(row)
                if self.paged: self.paged.begin(row,self.window)
            elif kind == 'page' and self.paged:
                self.paged.page(row)
            elif kind == 'query' and self.paged:
                self.paged.query(row)
            elif kind == 'shard':
                if self.paged: raise ValueError('logit shard in paged kernel')
                self.shard(row)
            elif kind == 'commit':
                yield from self.commit()
            else:
                raise ValueError('unknown tensor event')

    def begin(self, row):
        if self.window is not None or self.stopped or natural(row['position']) != self.position:
            raise ValueError('overlapping or stale draft window')
        if 'tree' in row:
            window=begin_tree(row,len(self.asset['vocabulary']))
            self.stats['scratch_pages']=self.reserve(len(window['tree']))
            self.stats['proposed']+=len(window['tree'])
            self.window=window
            return
        proposal = row['proposal']
        if not isinstance(proposal, list) or not 1 <= len(proposal) <= 8:
            raise ValueError('invalid proposal')
        size = len(self.asset['vocabulary'])
        for token in proposal:
            if natural(token) >= size:
                raise ValueError('unknown draft token')
        uniforms = row['uniforms']
        if not isinstance(uniforms, list) or len(uniforms) != len(proposal)+1:
            raise ValueError('wrong random tape length')
        if any(not 0 <= finite(x) < 1 for x in uniforms):
            raise ValueError('invalid random tape')
        self.stats['scratch_pages'] = self.reserve(len(proposal))
        self.stats['proposed'] += len(proposal)
        self.window = dict(proposal=proposal, uniforms=uniforms, shards={})

    def shard(self, row):
        if self.window is None:
            raise ValueError('shard without window')
        if 'tree' in self.window:
            shard_tree(row,self.window,self.asset)
            return
        rank = natural(row['rank'])
        if rank >= len(self.asset['shards']):
            raise ValueError('unknown tensor rank')
        width = len(self.asset['shards'][rank]['token_ids'])
        target, draft = row['target'], row['draft']
        length = len(self.window['proposal'])
        for matrix, height in [(target, length+1), (draft, length)]:
            if not isinstance(matrix, list) or len(matrix) != height:
                raise ValueError('matrix row mismatch')
            for vector in matrix:
                if not isinstance(vector, list) or len(vector) != width:
                    raise ValueError('matrix shard width mismatch')
                for value in vector:
                    if natural(value) > 255:
                        raise ValueError('invalid quantized logit')
        pair = (target, draft)
        existing = self.window['shards'].get(rank)
        if existing is not None and existing != pair:
            raise ValueError('conflicting tensor rank retransmission')
        self.window['shards'][rank] = pair

    def commit(self):
        if self.paged:
            for shard in self.paged.prepare(): self.shard(shard)
        current = self.window
        if current is None or set(current['shards']) != set(range(len(self.asset['shards']))):
            raise ValueError('incomplete tensor window')
        if 'tree' in current:
            return self.commit_tree()
        length = len(current['proposal'])
        matrices = [[[0.]*len(self.asset['vocabulary']) for _ in range(n)] for n in [length+1, length]]
        for rank, shard in enumerate(self.asset['shards']):
            for kind in range(2):
                for offset, values in enumerate(current['shards'][rank][kind]):
                    for index, token in enumerate(shard['token_ids']):
                        matrices[kind][offset][token] = (values[index]-shard['zero_point'])*shard['scale']
        staged = copy.deepcopy(self.state)
        emitted = []
        accepted, corrected, text = 0, 0, ''
        for index, token in enumerate(current['proposal']):
            target = distribution(matrices[0][index], self.options, staged)
            draft = distribution(matrices[1][index], self.options, staged)
            if draft[token] <= 0:
                raise ValueError('draft token has zero proposal probability')
            if current['uniforms'][index] < min(1., target[token]/draft[token]):
                emitted.append(token); accepted += 1
            else:
                residual = [max(0., p-q) for p,q in zip(target, draft)]
                total = sum(residual)
                probabilities = [x/total for x in residual] if total > 0 else target
                emitted.append(draw(probabilities, current['uniforms'][-1])); corrected = 1
                text += staged.append(emitted[-1], self.limit)
                break
            text += staged.append(token, self.limit)
            if staged.reason is not None:
                break
        else:
            if self.position+len(emitted) < self.limit:
                target = distribution(matrices[0][-1], self.options, staged)
                emitted.append(draw(target, current['uniforms'][-1])); corrected = 1
                text += staged.append(emitted[-1], self.limit)
        return self.publish(staged,emitted,accepted,corrected,text)

    def commit_tree(self):
        current=self.window; staged=copy.deepcopy(self.state)
        vertex=0; emitted=[]; accepted=corrected=0; text=''
        while True:
            logits=[0.]*len(self.asset['vocabulary'])
            for rank,part in enumerate(self.asset['shards']):
                for column,token in enumerate(part['token_ids']):
                    logits[token]=(current['shards'][rank][vertex][column]-part['zero_point'])*part['scale']
            token=draw(distribution(logits,self.options,staged),current['uniforms'][str(vertex)])
            child=current['children'].get((vertex,token))
            accepted+=child is not None; corrected+=child is None
            emitted.append(token); text+=staged.append(token,self.limit)
            if staged.reason is not None or child is None: break
            vertex=child
        return self.publish(staged,emitted,accepted,corrected,text)

    def publish(self,staged,emitted,accepted,corrected,text):
        if self.paged: self.paged.publish()
        self.state = staged
        self.decoding.clear(); self.decoding.update(staged.report())
        self.position += len(emitted)
        self.stopped = staged.reason is not None
        self.stats['accepted'] += min(accepted, len(emitted))
        self.stats['corrected'] += corrected
        self.stats['committed'] += len(emitted)
        self.stats['windows'] += 1
        self.release()
        self.stats['scratch_pages'] = 0
        self.window = None
        self.sequence += 1
        return [('data: '+json.dumps(dict(choices=[dict(text=text, token_ids=emitted)]), ensure_ascii=False)+'\n\n').encode()]

    def close(self):
        self.wire.decode(b'', final=True)
        if any(line.strip() and not line.startswith(':') for line in self.buffer.split('\n')) or not self.done:
            raise ValueError('truncated tensor stream')

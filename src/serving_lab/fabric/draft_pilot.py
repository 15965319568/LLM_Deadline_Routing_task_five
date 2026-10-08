"""Decode adapter originally deployed with the single-worker draft prototype."""
import json
import math
from pathlib import Path
from serving_lab.storage import read_json


def rank_distribution(values, metadata):
    logits=[(v-metadata['zero_point'])*metadata['scale'] for v in values]
    largest=max(logits)
    weights=[math.exp(v-largest) for v in logits]
    return [weight/sum(weights) for weight in weights]


class DraftPilot:
    def __init__(self, source, config, resource, limit):
        catalogue=read_json(Path(source)/config['decode_assets'])
        identity=config['resources'][resource]['deployment_id']
        self.asset=catalogue[identity]
        self.limit=limit; self.buffer=''; self.proposal=[]; self.rank_scores={}
    def feed(self, chunk):
        self.buffer+=chunk.decode('utf-8',errors='ignore')
        result=[]
        while '\n\n' in self.buffer:
            event,self.buffer=self.buffer.split('\n\n',1)
            if not event.startswith('data:'): continue
            text=event[5:].strip()
            if text=='[DONE]':
                result.append(b'data: [DONE]\n\n'); continue
            row=json.loads(text)
            if row['kind']=='begin': self.proposal=row['proposal']
            elif row['kind']=='shard':
                self.rank_scores[row['rank']]=[rank_distribution(v,self.asset['shards'][row['rank']]) for v in row['target']]
            elif row['kind']=='commit':
                tokens=self.proposal[:self.limit]
                text=b''.join(bytes.fromhex(self.asset['vocabulary'][token]) for token in tokens).decode('utf-8',errors='replace')
                result.append(('data: '+json.dumps(dict(choices=[dict(text=text,token_ids=tokens)]),ensure_ascii=False)+'\n\n').encode())
                self.limit-=len(tokens); self.proposal=[]; self.rank_scores={}
        return result

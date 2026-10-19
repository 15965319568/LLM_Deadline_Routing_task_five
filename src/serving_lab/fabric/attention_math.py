"""Grouped query attention over a logical KV table."""
import math


class AttentionMath:
    def rotate(self, values, position):
        result = []
        for i in range(0,len(values),2):
            angle = position/self.spec['rope_scale']/(self.spec['rope_theta']**(i/self.spec['head_dim']))
            a,b = values[i:i+2]
            result.extend([a*math.cos(angle)-b*math.sin(angle),a*math.sin(angle)+b*math.cos(angle)])
        return result

    def logits(self, kind, node, pages):
        spec = self.spec
        pos = self.stage['positions'][node]
        table = [r for r in self.stage['table'] if r['position']<=pos and
                 (r['position']>=pos-spec['window_size']+1 or r['position']<spec['sink_tokens'])]
        if not table:
            raise ValueError('empty causal attention context')
        context = [None]*len(spec['head_map'])
        dim = spec['head_dim']
        for rank, part in enumerate(spec['ranks']):
            vectors = self.stage['queries'][rank][kind][node]
            for local,head in enumerate(part['query_heads']):
                query = self.rotate([(x-part['zero_point'])*part['scale'] for x in vectors[local]],pos)
                scores, values = [], []
                kvhead = head * spec['kv_heads'] // len(spec['head_map'])
                for row in table:
                    page = pages[row['page_id'],row['version']]
                    offset = (row['slot']*spec['kv_heads']+kvhead)*dim
                    channel = kvhead*dim
                    key = [(page['k'][offset+i]-page['k_zero'][channel+i])*page['k_scale'][channel+i] for i in range(dim)]
                    val = [(page['v'][offset+i]-page['v_zero'][channel+i])*page['v_scale'][channel+i] for i in range(dim)]
                    key = self.rotate(key,row['slot'])
                    scores.append(sum(a*b for a,b in zip(query,key))/math.sqrt(dim)+spec['alibi_slopes'][head]*(row['position']-pos))
                    values.append(val)
                maximum = max(scores)
                weights = [math.exp(s-maximum) for s in scores]
                total = sum(weights)
                context[head] = [sum(w*v[i] for w,v in zip(weights,values))/total for i in range(dim)]
        flattened = [x for part in spec['ranks'] for head in part['query_heads'] for x in context[head]]
        return [spec['bias'][token]+sum((w-spec['projection_zero'][token])*spec['projection_scale'][token]*x
                                       for w,x in zip(row,flattened)) for token,row in enumerate(spec['projection'])]


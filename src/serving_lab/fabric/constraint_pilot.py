"""Registry preview used by the first structured-output rollout."""
import csv
import json
from pathlib import Path
from serving_lab.storage import read_json


def preview(root, config, cutoff):
    if not config.get('grammar_exports'): return None
    root=Path(root); records={}; ledger=[]
    for export in read_json(root/config['grammar_exports'])['sources']:
        if export['format']!='csv': continue
        with (root/export['path']).open(encoding='utf-8') as stream:
            for index,row in enumerate(csv.DictReader(stream),1):
                try:
                    if int(row['event_us'])>cutoff: continue
                    key=row['grammar_id'],int(row['revision'])
                    records.setdefault(key,{})[row['part']]=json.loads(row['payload'])
                    ledger.append(dict(source=export['path']+'#'+str(index),disposition='selected'))
                except (KeyError,ValueError): continue
    grammars={}
    for (gid,revision),parts in sorted(records.items()):
        if 'metadata' not in parts: continue
        item=parts['metadata']; edges={}
        for row in parts.get('arcs',[]): edges[row['from'],row['byte']]=row['to']
        grammars[gid]=dict(item,revision=revision,edges=[dict(zip(['from','byte','to'],[s,b,t])) for (s,b),t in sorted(edges.items())])
    return dict(as_of_us=cutoff,grammars=grammars,ledger=sorted(ledger,key=lambda row:row['source']))

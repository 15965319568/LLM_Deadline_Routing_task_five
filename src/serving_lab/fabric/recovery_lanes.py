"""Worker repair lane adapter for systematic DMA uploads."""
def ingest(entry,row):
    entry.setdefault('repairs',{})[row['id']]=dict(row)

def materialize(entry):
    values={}
    for row in entry.get('repairs',{}).values():
        terms=row.get('terms',[])
        if len(terms)==1:
            term=terms[0]
            values[term['index']]=row['value']//term['coefficient']
    return values

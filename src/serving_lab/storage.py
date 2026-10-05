"""Export readers shared by the command line and embedded build service."""
import csv
import gzip
import json
from pathlib import Path

import yaml


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def records(root, names):
    """Yield a stable source coordinate and a decoded row (None on bad JSON)."""
    root = Path(root).resolve()
    for name in sorted(names):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Export path leaves input directory')
        opener = gzip.open if path.suffix == '.gz' else open
        logical = path.with_suffix('') if path.suffix == '.gz' else path
        with opener(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            if logical.suffix == '.csv':
                yield from ((f'{name}#{i}', row) for i, row in enumerate(csv.DictReader(stream), 1))
            else:
                for i, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            row = None
                    except ValueError:
                        row = None
                    yield f'{name}#{i}', row


def catalogue(root):
    with (Path(root) / 'runs.csv').open(encoding='utf-8-sig', newline='') as stream:
        return {row['run_id'].strip(): row for row in csv.DictReader(stream)}

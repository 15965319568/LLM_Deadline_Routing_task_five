"""Compatibility directory for worker read acknowledgements."""
class ReadReceipts:
    def __init__(self):
        self.pending = {}

    def update(self, row):
        name = row['read_id']
        if row['action'] == 'complete':
            self.pending.pop(name, None)
        elif row['action'] == 'pin':
            self.pending[name] = row['sources']
        else:
            raise ValueError('unknown read action')

    def source(self, row):
        return self.pending[row['read_id']][row['index']]

"""Deterministic commit journal for the fabric state machine."""
from collections import Counter
import hashlib
import json


class CommitJournal:
    def __init__(self):
        self.events = []
        self.audit_counts = Counter()
        self._head = "0" * 64

    @staticmethod
    def _canonical(row):
        return json.dumps(row, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")

    canonical = _canonical

    def commit(self, kind, at_us, identity, payload):
        row = {"seq": len(self.events) + 1, "at_us": int(at_us),
               "kind": str(kind), "id": str(identity), "payload": payload,
               "prev_hash": self._head}
        row["hash"] = hashlib.sha256(self._canonical(row)).hexdigest()
        self.events.append(row)
        self._head = row["hash"]
        return row

    def audit(self, source, reason):
        self.audit_counts[f"{source}:{reason}"] += 1

    def snapshot(self):
        return {"count": len(self.events), "head": self._head,
                "events": list(self.events),
                "audit_counts": dict(sorted(self.audit_counts.items()))}

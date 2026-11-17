"""Raw event recording with immutable IDs and byte digests."""
from __future__ import annotations

import hashlib
import json
import sqlite3


class EvidenceConflict(ValueError):
    pass


def canonical(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def record(db: sqlite3.Connection, event: dict, kind: str, object_id: str) -> bool:
    event_id = str(event['event_id'])
    body = canonical(event)
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    old = db.execute('SELECT sha256 FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        if old['sha256'] != digest:
            raise EvidenceConflict(f'event_id {event_id} has different content')
        return False
    db.execute('INSERT INTO evidence(event_id,kind,object_id,event_time,sha256,body) VALUES (?,?,?,?,?,?)',
               (event_id,kind,object_id,event['event_time'],digest,body))
    return True

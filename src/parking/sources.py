"""Raw event recording with immutable IDs and byte digests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone


class EvidenceConflict(ValueError):
    pass


def canonical(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _captured_at(event: dict) -> str:
    """Import time, used only when the event omits its own `captured_at`."""
    return event.get('captured_at') or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


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
               (event_id, kind, object_id, event.get('event_time') or _captured_at(event), digest, body))
    return True


def screen(db: sqlite3.Connection, events: list[dict]) -> list[dict]:
    """Read-only pre-flight check for event-ID reuse with different content.

    A conflicting batch is rejected atomically, but the conflicting body is not
    discarded: callers persist it through `save_conflicts` before raising.
    """
    conflicts = []
    seen: dict[str, tuple[str, str]] = {}
    for event in events:
        event_id = str(event['event_id'])
        body = canonical(event)
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        old = db.execute('SELECT sha256 FROM evidence WHERE event_id=?', (event_id,)).fetchone()
        if old and old['sha256'] != digest:
            conflicts.append({'event_id': event_id, 'existing_sha256': old['sha256'],
                              'incoming_sha256': digest, 'incoming_body': body})
        elif event_id in seen and seen[event_id][0] != digest:
            conflicts.append({'event_id': event_id, 'existing_sha256': seen[event_id][0],
                              'incoming_sha256': digest, 'incoming_body': body})
        seen.setdefault(event_id, (digest, body))
    return conflicts


def save_conflicts(db: sqlite3.Connection, conflicts: list[dict]) -> None:
    for c in conflicts:
        db.execute('INSERT INTO evidence_conflict(event_id,existing_sha256,incoming_sha256,incoming_body) VALUES (?,?,?,?)',
                   (c['event_id'], c['existing_sha256'], c['incoming_sha256'], c['incoming_body']))


def reject_conflicts(db: sqlite3.Connection, events: list[dict]) -> None:
    conflicts = screen(db, events)
    if conflicts:
        with db:
            save_conflicts(db, conflicts)
        raise EvidenceConflict(f'event_id {conflicts[0]["event_id"]} has different content')


def conflicts(db: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in db.execute(
        'SELECT conflict_id,event_id,existing_sha256,incoming_sha256,incoming_body,detected_at '
        'FROM evidence_conflict ORDER BY conflict_id').fetchall()]

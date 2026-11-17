"""Recompute availability from independent evidence ledgers."""
from __future__ import annotations

import json
from . import sensor,gate,capacity
from .store import rows


def decide(db,as_of: str,total_bays: int,timeout_minutes: int=30,save: bool=True) -> dict:
    if total_bays<0:raise ValueError('total_bays cannot be negative')
    s=sensor.snapshot(db,as_of,timeout_minutes)
    g=gate.snapshot(db,as_of)
    c=capacity.snapshot(db,as_of)
    usable=max(0,total_bays-c['reserved_count'])
    discrepancy=None
    if s['unknown']==0 and s['states']:
        discrepancy=s['occupied']-g['active']
    if s['unknown'] and not g['complete']:
        state='unknown';available=None;reason='sensor_and_gate_incomplete'
    elif g['orphan']:
        state='frozen';available=None;reason='orphan_exit'
    elif s['unknown']:
        state='conservative';available=max(0,usable-g['active']);reason='gate_only_bound'
    elif discrepancy is not None and discrepancy!=0:
        state='frozen';available=None;reason='sensor_gate_discrepancy'
    else:
        state='published';available=max(0,usable-max(s['occupied'],g['active']));reason='reconciled'
    result={'as_of':as_of,'total_bays':total_bays,'usable_bays':usable,'available':available,'state':state,'reason':reason,
            'sensor':s,'gate':g,'capacity':c,'discrepancy':discrepancy}
    if save:
        with db:
            db.execute('INSERT INTO availability_release(as_of,available,state,evidence) VALUES (?,?,?,?)',
                       (as_of,available,state,json.dumps(result,sort_keys=True)))
    return result


def history(db) -> list[dict]:
    return rows(db,'SELECT release_id,as_of,available,state,evidence FROM availability_release ORDER BY release_id')

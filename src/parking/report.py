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
    h=capacity.handover_snapshot(db,as_of,timeout_minutes,save=save)
    uncovered=max(0,total_bays-len(s['states']))
    s['uncovered']=uncovered
    s['unknown']+=uncovered
    usable=max(0,total_bays-c['reserved_count'])
    capacity_breach=g['active']>usable
    discrepancy=None
    if s['unknown']==0 and s['states']:
        discrepancy=s['occupied']-g['active']
    if capacity_breach:
        state='capacity_breach';available=None;reason='active_stays_exceed_operable_capacity'
    elif s['unknown'] and not g['complete']:
        state='unknown';available=None;reason='sensor_and_gate_incomplete'
    elif not s['states'] and not g['complete']:
        state='unknown';available=None;reason='no_occupancy_evidence'
    elif g['orphan']:
        state='frozen';available=None;reason='orphan_exit'
    elif g['duplicate']:
        state='frozen';available=None;reason='duplicate_gate_receipt'
    elif s['unknown']:
        state='conservative';available=max(0,usable-g['active']);reason='gate_only_bound'
    elif discrepancy is not None and discrepancy!=0:
        state='frozen';available=None;reason='sensor_gate_discrepancy'
    else:
        state='published';available=max(0,usable-max(s['occupied'],g['active']));reason='reconciled'
    result={'as_of':as_of,'total_bays':total_bays,'usable_bays':usable,'available':available,'state':state,'reason':reason,
            'capacity_breach':capacity_breach,
            'sensor':s,'gate':g,'capacity':c,'handover':h,'discrepancy':discrepancy}
    if save:
        with db:
            if capacity_breach:
                db.execute('INSERT INTO capacity_incident(as_of,active_lower_bound,operable_capacity,decision,reason) VALUES (?,?,?,?,?)',
                           (as_of,g['active'],usable,'operator_investigation','gate lower bound exceeds capacity after authorized reservations'))
            db.execute('INSERT INTO availability_release(as_of,available,state,evidence) VALUES (?,?,?,?)',
                       (as_of,available,state,json.dumps(result,sort_keys=True)))
    return result


def history(db) -> list[dict]:
    return rows(db,'SELECT release_id,as_of,available,state,evidence FROM availability_release ORDER BY release_id')

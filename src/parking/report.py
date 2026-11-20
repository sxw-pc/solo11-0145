"""Recompute availability from independent evidence ledgers."""
from __future__ import annotations

import json
from . import sensor,gate,capacity
from .store import rows


def decide(db,as_of: str,total_bays: int,timeout_minutes: int=30,save: bool=True,
           diagnose_dispatch: bool=False,max_faults: int=2,vehicle_capacity: int=3,
           risk_threshold: float=0.75) -> dict:
    if total_bays<0:raise ValueError('total_bays cannot be negative')
    diagnosis=None
    if diagnose_dispatch:
        # Imported lazily so availability reporting stays usable without the
        # maintenance topology; a missing topology is an input error, not a
        # reason to silently drop the diagnosis attachment.
        from . import maintenance
        diagnosis=maintenance.diagnose(db,as_of,timeout_minutes=timeout_minutes,max_faults=max_faults,
                                       vehicle_capacity=vehicle_capacity,risk_threshold=risk_threshold,save=save)
    s=sensor.snapshot(db,as_of,timeout_minutes,
                      diagnostic_causes={bay:{"reason":c["reason"],"nodes":c["nodes"],"device_id":c["device_id"]}
                                         for bay,c in (diagnosis['bay_causes'].items() if diagnosis else [])})
    g=gate.snapshot(db,as_of)
    c=capacity.snapshot(db,as_of)
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
            'sensor':s,'gate':g,'capacity':c,'discrepancy':discrepancy}
    if diagnosis is not None:
        # Diagnosis is an attachment: it explains causes and dispatch, but the
        # conservative availability state and the unknown bays are unchanged.
        result['diagnosis']={'topology_event':diagnosis['topology_event'],
                             'explanation_count':diagnosis['explanation_count'],
                             'explanations':diagnosis.get('explanations',[]),
                             'eliminated':diagnosis['eliminated'],
                             'explanation_limit_reached':diagnosis['explanation_limit_reached'],
                             'confirmed_nodes':diagnosis['confirmed_nodes'],
                             'bay_causes':diagnosis['bay_causes'],
                             'guaranteed_repair':diagnosis['guaranteed_repair'],
                             'risk_budget':diagnosis['risk_budget'],
                             'frequency_complete':diagnosis['frequency_complete'],
                             'contracts_disagree':diagnosis['contracts_disagree'],
                             'revision':diagnosis.get('revision')}
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

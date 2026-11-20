"""Gate stay reconciliation and billable intervals."""
from __future__ import annotations

from .sources import record
from .store import rows


def ingest(db,events: list[dict]) -> dict:
    made=0
    with db:
        for e in events:
            if e.get('op')=='baseline':
                if int(e['count'])<0:raise ValueError('negative gate baseline')
                if record(db,e,'gate_baseline','lot'):
                    db.execute('INSERT INTO gate_baseline VALUES (?,?,?)',(e['event_id'],e['event_time'],int(e['count'])))
                    made+=1
            else:
                if e['direction'] not in ('in','out'):raise ValueError('gate direction must be in/out')
                if record(db,e,'gate',e['stay_id']):
                    db.execute('INSERT INTO gate_event VALUES (?,?,?,?,?)',(e['event_id'],e['stay_id'],e['direction'],e['event_time'],e.get('gate_id')))
                    made+=1
        reconcile(db)
    return {'imported':made,'stays':len(rows(db,'SELECT stay_id FROM vehicle_stay'))}


def reconcile(db):
    db.execute('DELETE FROM vehicle_stay')
    events=rows(db,'SELECT * FROM gate_event ORDER BY occurred_at,event_id')
    by_stay={}
    for e in events:by_stay.setdefault(e['stay_id'],[]).append(e)
    duplicates=0;orphans=0;active=0
    for stay_id,items in by_stay.items():
        ins=[e for e in items if e['direction']=='in']
        outs=[e for e in items if e['direction']=='out']
        if len(ins)>1 or len(outs)>1:duplicates+=max(0,len(ins)-1)+max(0,len(outs)-1)
        first_in=ins[0]['occurred_at'] if ins else None
        first_out=outs[0]['occurred_at'] if outs else None
        entry_gate=ins[0]['gate_id'] if ins else None
        exit_gate=outs[0]['gate_id'] if outs else None
        if not first_in or (first_out and first_out<first_in):
            state='orphan';reason='out_without_prior_in' if first_out else 'missing_in';orphans+=1
        elif first_out:
            state='closed';reason='paired'
        else:
            state='active';reason='entry_without_exit';active+=1
        db.execute('INSERT INTO vehicle_stay VALUES (?,?,?,?,?,?,?)',(stay_id,first_in,first_out,state,reason,entry_gate,exit_gate))
    db.execute("INSERT INTO gate_balance VALUES (1,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET active_count=excluded.active_count,orphan_count=excluded.orphan_count,duplicate_count=excluded.duplicate_count,computed_at=excluded.computed_at",(active,orphans,duplicates))


def snapshot(db,as_of: str) -> dict:
    baseline=db.execute('SELECT occurred_at,count,event_id FROM gate_baseline WHERE occurred_at<=? ORDER BY occurred_at DESC,event_id DESC LIMIT 1',(as_of,)).fetchone()
    events=rows(db,'SELECT * FROM gate_event WHERE occurred_at<=? ORDER BY occurred_at,event_id',(as_of,))
    by_stay={}
    for e in events:by_stay.setdefault(e['stay_id'],[]).append(e)
    active=0;orphan=0;duplicate=0;billable=[];delta=0
    for stay_id,items in sorted(by_stay.items()):
        ins=[e for e in items if e['direction']=='in'];outs=[e for e in items if e['direction']=='out']
        duplicate+=max(0,len(ins)-1)+max(0,len(outs)-1)
        if not ins or (outs and outs[0]['occurred_at']<ins[0]['occurred_at']):orphan+=1;continue
        if not outs:active+=1
        else:billable.append({'stay_id':stay_id,'from':ins[0]['occurred_at'],'to':outs[0]['occurred_at'],
                              'entry_gate':ins[0]['gate_id'],'exit_gate':outs[0]['gate_id']})
        if baseline:
            if ins[0]['occurred_at']>baseline['occurred_at']:delta+=1
            if outs and outs[0]['occurred_at']>baseline['occurred_at']:delta-=1
    if baseline:active=max(0,baseline['count']+delta)
    return {'active':active,'orphan':orphan,'duplicate':duplicate,'billable':billable,
            'baseline':dict(baseline) if baseline else None,'complete':baseline is not None and orphan==0 and duplicate==0}

"""Time-windowed capacity orders and set-union bay deductions."""
from __future__ import annotations

from .sources import record
from .store import rows

PURPOSES={'construction','fire','accessible'}


def ingest(db,events: list[dict]) -> dict:
    made=0
    with db:
        for e in events:
            if e['purpose'] not in PURPOSES:raise ValueError('invalid capacity purpose')
            if e['starts_at']>=e['ends_at']:raise ValueError('empty capacity window')
            if not isinstance(e['bays'],list) or not e['bays']:raise ValueError('capacity order needs bays')
            if record(db,e,'capacity_order',e['order_id']):
                old=db.execute('SELECT source_event FROM capacity_order WHERE order_id=?',(e['order_id'],)).fetchone()
                if old:
                    prior=db.execute('SELECT event_time FROM evidence WHERE event_id=?',(old['source_event'],)).fetchone()
                    if prior['event_time']>e['event_time']:
                        made+=1;continue
                    db.execute('DELETE FROM capacity_window WHERE order_id=?',(e['order_id'],))
                status='approved' if e.get('approved',False) and not e.get('revoked',False) else 'revoked' if e.get('revoked',False) else 'pending'
                db.execute('INSERT INTO capacity_order(order_id,purpose,priority,status,source_event) VALUES (?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET purpose=excluded.purpose,priority=excluded.priority,status=excluded.status,source_event=excluded.source_event',
                           (e['order_id'],e['purpose'],int(e['priority']),status,e['event_id']))
                for bay in sorted(set(e['bays'])):
                    db.execute('INSERT INTO capacity_window VALUES (?,?,?,?)',(e['order_id'],bay,e['starts_at'],e['ends_at']))
                db.execute('INSERT INTO capacity_decision VALUES (?,?,?) ON CONFLICT(order_id) DO UPDATE SET decision=excluded.decision,reason=excluded.reason',
                           (e['order_id'],status,'signed_order' if status=='approved' else 'not_currently_authorized'))
                made+=1
    return {'imported':made,'orders':len(rows(db,'SELECT order_id FROM capacity_order'))}


def snapshot(db,as_of: str) -> dict:
    active=rows(db,"SELECT c.order_id,c.purpose,c.priority,w.bay_id FROM capacity_order c JOIN capacity_window w USING(order_id) WHERE c.status='approved' AND w.starts_at<=? AND w.ends_at>? ORDER BY c.priority DESC,c.order_id",(as_of,as_of))
    bays=sorted({r['bay_id'] for r in active})
    # The highest priority order owns the operational response for a shared bay;
    # the bay is still deducted only once from public capacity.
    owners={}
    for r in active:
        owners.setdefault(r['bay_id'],{'order_id':r['order_id'],'purpose':r['purpose'],'priority':r['priority']})
    return {'reserved_bays':bays,'reserved_count':len(bays),'active_orders':sorted({r['order_id'] for r in active}),
            'owner_by_bay':owners}

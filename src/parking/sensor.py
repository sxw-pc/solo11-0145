"""Bay telemetry, calibration and late-event projection."""
from __future__ import annotations

from datetime import datetime, timezone
from .sources import record
from .store import rows


def ingest(db, events: list[dict]) -> dict:
    made = 0
    with db:
        for e in events:
            op = e['op']
            if op not in ('device', 'reading'):
                raise ValueError(f'unknown sensor op {op}')
            if op == 'device':
                if record(db,e,'sensor_device',e['device_id']):
                    old=db.execute('SELECT valid_from FROM sensor_registry WHERE device_id=?',(e['device_id'],)).fetchone()
                    if old is None or e['event_time'] >= old['valid_from']:
                        db.execute('INSERT INTO sensor_registry(device_id,bay_id,calibrated_until,valid_from,source_event) VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET bay_id=excluded.bay_id,calibrated_until=excluded.calibrated_until,valid_from=excluded.valid_from,source_event=excluded.source_event',
                                   (e['device_id'],e['bay_id'],e['calibrated_until'],e['event_time'],e['event_id']))
                    made += 1
            else:
                if record(db,e,'sensor_reading',e['bay_id']):
                    db.execute('INSERT INTO occupancy_event VALUES (?,?,?,?,?,?)',
                               (e['event_id'],e['device_id'],e['bay_id'],e['event_time'],int(e['sequence_no']),int(bool(e['occupied']))))
                    made += 1
        for bay in [r['bay_id'] for r in rows(db,'SELECT DISTINCT bay_id FROM occupancy_event')]:
            state=bay_state(db,bay,max(e['event_time'] for e in events))
            db.execute('INSERT INTO bay_projection(bay_id,state,selected_event,observed_at,reason) VALUES (?,?,?,?,?) ON CONFLICT(bay_id) DO UPDATE SET state=excluded.state,selected_event=excluded.selected_event,observed_at=excluded.observed_at,reason=excluded.reason',
                       (bay,state['state'],state['event_id'],state['observed_at'],state['reason']))
    return {'imported':made,'bays':len(rows(db,'SELECT bay_id FROM bay_projection'))}


def bay_state(db,bay_id: str,as_of: str,timeout_minutes: int=30) -> dict:
    candidates=rows(db,'SELECT o.*,r.bay_id AS registered_bay,r.calibrated_until,r.valid_from FROM occupancy_event o LEFT JOIN sensor_registry r ON r.device_id=o.device_id WHERE o.bay_id=? AND o.observed_at<=? ORDER BY o.observed_at DESC,o.sequence_no DESC,o.event_id DESC',(bay_id,as_of))
    if not candidates:
        return {'state':'unknown','event_id':None,'observed_at':None,'reason':'no_reading'}
    e=candidates[0]
    if e['registered_bay']!=bay_id or e['observed_at']>e['calibrated_until'] or e['observed_at']<e['valid_from']:
        return {'state':'unknown','event_id':e['event_id'],'observed_at':e['observed_at'],'reason':'untrusted_device'}
    elapsed=(datetime.fromisoformat(as_of.replace('Z','+00:00'))-datetime.fromisoformat(e['observed_at'].replace('Z','+00:00'))).total_seconds()/60
    if elapsed>timeout_minutes:
        return {'state':'unknown','event_id':e['event_id'],'observed_at':e['observed_at'],'reason':'heartbeat_timeout'}
    return {'state':'occupied' if e['occupied'] else 'free','event_id':e['event_id'],'observed_at':e['observed_at'],'reason':'trusted_reading'}


def snapshot(db,as_of: str,timeout_minutes: int=30) -> dict:
    bays={r['bay_id'] for r in rows(db,'SELECT bay_id FROM sensor_registry')}
    bays.update(r['bay_id'] for r in rows(db,'SELECT DISTINCT bay_id FROM occupancy_event'))
    states={bay:bay_state(db,bay,as_of,timeout_minutes) for bay in sorted(bays)}
    return {'states':states,'free':sum(v['state']=='free' for v in states.values()),'occupied':sum(v['state']=='occupied' for v in states.values()),'unknown':sum(v['state']=='unknown' for v in states.values())}
